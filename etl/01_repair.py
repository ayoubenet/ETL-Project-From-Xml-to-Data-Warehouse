"""
Step 1 - Repair raw PubMed XML files.

Reads every file of the raw data folder (never modified), fixes encoding and
structural corruption, and writes well-formed UTF-8 copies to staging/clean_xml/.
Every file gets one line in staging/repair_log.csv:

    status = ok        parsed as-is (only BOM / encoding normalised)
             repaired  structural fixes applied, record is complete
             partial   repaired, but part of the record was lost (kept, flagged)
             rejected  no usable article (no PMID or no title) -> not copied

Usage: python etl/01_repair.py [raw_dir] [out_dir]
"""
import csv
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "Data"
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "staging"
CLEAN_DIR = OUT_DIR / "clean_xml"

TAG_RE = re.compile(r"<(/?)([A-Za-z_][\w.-]*)([^<>]*?)(/?)>")


def decode(raw: bytes):
    """Return (text, encoding). Files are UTF-8 (mostly with BOM) or Windows-1252."""
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def try_parse(text):
    try:
        return ET.fromstring(text), None
    except ET.ParseError as e:
        return None, str(e)


def fix_known_patterns(text, fixes):
    """Targeted fixes for the corruption patterns observed in the corpus."""
    # Missing '<' before the root tag: "PubmedArticleSet>"
    if text.lstrip().startswith("PubmedArticleSet>"):
        text = "<" + text.lstrip()
        fixes.append("missing_root_lt")

    # Wrapper lines missing at the start of the file
    head = text.lstrip()
    if head.startswith("<MedlineCitation"):
        text = "<PubmedArticleSet>\n<PubmedArticle>\n" + head
        fixes.append("missing_root_and_article_open")
    elif head.startswith("<PubmedArticle>"):
        text = "<PubmedArticleSet>\n" + head
        fixes.append("missing_root_open")

    # Root opening tag duplicated
    new = re.sub(r"^(\s*<PubmedArticleSet>)(\s*<PubmedArticleSet>)+", r"\1", text, count=1)
    if new != text:
        text = new
        fixes.append("duplicated_root")

    # A "<PubmedArticle>" line replaced by "</PubmedArticleSet>" (start of file or
    # between two records): recognisable because a <MedlineCitation> follows it.
    new = re.sub(r"</PubmedArticleSet>(\s*<MedlineCitation\b)", r"<PubmedArticle>\1", text)
    if new != text:
        text = new
        fixes.append("closed_root_instead_of_article")

    # Unescaped '<' / '&' inside text ("PSA <0.5 ng/ml")
    new = re.sub(r"<(?![A-Za-z_/?!])", "&lt;", text)
    new = re.sub(r"&(?!#?\w+;)", "&amp;", new)
    if new != text:
        text = new
        fixes.append("escaped_special_chars")

    # Junk after the closing root tag ("</PubmedArticleSet>.")
    new = re.sub(r"(</PubmedArticleSet>)\s*[^<\s][^<]*$", r"\1\n", text)
    if new != text:
        text = new
        fixes.append("junk_after_root")

    # Truncated / garbage tail: keep everything up to the last complete record
    # and close the root properly.
    stripped = text.rstrip()
    if not stripped.endswith("</PubmedArticleSet>"):
        last = max(stripped.rfind("</PubmedArticle>"), stripped.rfind("</PubmedBookArticle>"))
        tail = stripped[last:] if last != -1 else stripped
        # tail after the last full record contains only whitespace + junk open/partial tags
        if last != -1 and re.fullmatch(r"</Pubmed(Book)?Article>\s*(<[^>]*>?\s*)*", tail):
            end = stripped.index(">", last) + 1
            text = stripped[:end] + "\n</PubmedArticleSet>\n"
            fixes.append("bad_tail")
    return text


def balance_tags(text, fixes):
    """Generic recovery: drop orphan closing tags, close tags left open, cut partial tags."""
    text = re.sub(r"<[^<>]*$", "", text.rstrip())  # partial tag at end of file
    out, stack, pos = [], [], 0
    dropped = closed = 0
    for m in TAG_RE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        closing, name, selfclose = m.group(1), m.group(2), m.group(4)
        if text[m.start():m.start() + 2] in ("<?", "<!"):
            out.append(m.group(0))
        elif selfclose:
            out.append(m.group(0))
        elif not closing:
            stack.append(name)
            out.append(m.group(0))
        elif name in stack:
            # close any element left open inside this one (lost closing tags)
            while stack[-1] != name:
                out.append(f"</{stack.pop()}>")
                closed += 1
            stack.pop()
            out.append(m.group(0))
        else:
            dropped += 1  # orphan closing tag (its opening part was lost)
    out.append(text[pos:])
    while stack:
        out.append(f"</{stack.pop()}>")
        closed += 1
    if dropped:
        fixes.append(f"dropped_orphan_close_tags:{dropped}")
    if closed:
        fixes.append(f"closed_open_tags:{closed}")
    return "".join(out)


def drop_stray_text(root):
    """Text left between structural elements after a lost block is meaningless: remove it."""
    removed = 0
    for el in root.iter():
        if len(el) and el.text and el.text.strip():
            el.text, removed = "\n", removed + 1
        for child in el:
            if child.tail and child.tail.strip():
                child.tail, removed = "\n", removed + 1
    return removed


def any_pmid(el):
    """Best-effort PMID of a (possibly damaged) record, for logging."""
    for node in el.iter():
        if node.tag == "ArticleId" and node.get("IdType") == "pubmed":
            return (node.text or "").strip()
    pmid = el.findtext(".//PMID")
    return (pmid or "?").strip()


def check_records(root, damaged):
    """Drop unusable records from root. Return (kept pmids, dropped pmids, problems).

    A PubmedArticle is usable if it still has MedlineCitation/PMID and a title.
    Missing sections are only reported as problems when the file was damaged
    (an intact record without AuthorList is legitimate).
    """
    kept, dropped, problems = [], [], []
    for art in list(root):
        if art.tag == "PubmedArticle":
            mc = art.find("MedlineCitation")
            pmid = art.findtext("MedlineCitation/PMID")
            title = art.findtext("MedlineCitation/Article/ArticleTitle")
            if not (pmid or "").strip() or not (title or "").strip():
                dropped.append(any_pmid(art))
                root.remove(art)
                continue
            kept.append(pmid.strip())
            if damaged:
                for path, label in (("Article/Journal", "missing_journal"),
                                    ("Article/AuthorList", "missing_authorlist")):
                    if mc.find(path) is None:
                        problems.append(label)
                if art.find("PubmedData") is None:
                    problems.append("missing_pubmeddata")
        elif art.tag == "PubmedBookArticle":
            kept.append((art.findtext("BookDocument/PMID") or "?").strip())
        else:
            dropped.append(any_pmid(art))
            root.remove(art)
            problems.append(f"unexpected_element:{art.tag}")
    return kept, dropped, problems


def repair_file(path):
    raw = path.read_bytes()
    text, encoding = decode(raw)
    fixes = [] if encoding == "utf-8" else ["reencoded_cp1252"]
    # error as the raw bytes would be seen by a UTF-8 parser
    original_error = try_parse(raw)[1]

    root, err = try_parse(text)
    if root is None:
        text = fix_known_patterns(text, fixes)
        root, err = try_parse(text)
    if root is None:
        text = balance_tags(text, fixes)
        root, err = try_parse(text)
    if root is None:
        return dict(status="rejected", encoding=encoding, fixes=fixes, error=original_error,
                    final_error=err, pmids=[], dropped=[], problems=[]), None

    damaged = any(f.startswith(("dropped_orphan", "closed_open")) for f in fixes)
    if damaged:
        n = drop_stray_text(root)
        if n:
            fixes.append(f"removed_stray_text:{n}")

    kept, dropped, problems = check_records(root, damaged)
    if not kept:
        status = "rejected"
    elif damaged or dropped or problems:
        status = "partial"
    elif fixes:
        status = "repaired"
    else:
        status = "ok"
    return dict(status=status, encoding=encoding, fixes=fixes, error=original_error,
                final_error=None, pmids=kept, dropped=dropped, problems=problems), \
        (root if status != "rejected" else None)


def main():
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(RAW_DIR.glob("*.xml"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
    stats = Counter()
    with open(OUT_DIR / "repair_log.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "status", "encoding", "n_records", "pmids", "dropped_pmids", "fixes",
                    "problems", "original_error", "final_error"])
        for path in files:
            info, root = repair_file(path)
            stats[info["status"]] += 1
            out = CLEAN_DIR / path.name
            if root is not None:
                ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
            elif out.exists():
                out.unlink()
            w.writerow([path.name, info["status"], info["encoding"], len(info["pmids"]),
                        "|".join(info["pmids"]), "|".join(info["dropped"]), "|".join(info["fixes"]),
                        "|".join(sorted(set(info["problems"]))),
                        info["error"] or "", info["final_error"] or ""])

    print(f"{len(files)} files processed from {RAW_DIR}")
    for k in ("ok", "repaired", "partial", "rejected"):
        print(f"  {k:9s} {stats[k]}")
    print(f"clean files -> {CLEAN_DIR}\nlog         -> {OUT_DIR / 'repair_log.csv'}")


if __name__ == "__main__":
    main()

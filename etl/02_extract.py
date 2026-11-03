"""
Step 2 - Extract the repaired XML into flat staging tables.

Reads staging/clean_xml/ (output of step 1) and writes one CSV per entity to
staging/tables/. Values are normalised (trimmed text, ISO dates, month names to
numbers) but not yet aggregated: this is the input of the warehouse load.

    articles.csv            one row per PMID (duplicates across files are skipped)
    authors.csv             article x author, with position and first affiliation
    mesh_headings.csv       article x MeSH descriptor (+ qualifiers, major topic flag)
    keywords.csv            article x author keyword
    publication_types.csv   article x publication type
    chemicals.csv           article x chemical substance
    grants.csv              article x grant (agency, country)
    duplicates.csv          PMIDs found in more than one file

Usage: python etl/02_extract.py [staging_dir]
"""
import csv
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "staging"
CLEAN_DIR = STAGING / "clean_xml"
OUT_DIR = STAGING / "tables"

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}

TABLES = {
    "articles": ["pmid", "source_file", "repair_status", "title", "language", "pub_year",
                 "pub_month", "pub_day", "pub_date", "pub_date_raw", "created_date",
                 "completed_date", "journal_nlm_id", "journal_title", "journal_iso",
                 "journal_issn", "journal_issn_linking", "journal_country", "volume", "issue",
                 "pages", "doi", "pub_model", "publication_status", "medline_status",
                 "n_authors", "n_mesh", "n_keywords", "n_references", "has_abstract",
                 "abstract_length"],
    "authors": ["pmid", "position", "is_first", "is_last", "last_name", "fore_name",
                "initials", "collective_name", "author_key", "affiliation"],
    "mesh_headings": ["pmid", "descriptor_ui", "descriptor", "major_topic", "qualifiers"],
    "keywords": ["pmid", "keyword", "major_topic"],
    "publication_types": ["pmid", "type_ui", "publication_type"],
    "chemicals": ["pmid", "registry_number", "substance_ui", "substance"],
    "grants": ["pmid", "grant_id", "acronym", "agency", "country"],
    "duplicates": ["pmid", "kept_file", "skipped_file"],
}


def txt(el, path=None):
    """Whitespace-normalised text of el (or of el.find(path)), '' if absent."""
    node = el.find(path) if path else el
    if node is None:
        return ""
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def to_int(s):
    return int(s) if s and s.isdigit() else None


def month_num(s):
    s = (s or "").strip().lower()
    if s.isdigit():
        return int(s) if 1 <= int(s) <= 12 else None
    return MONTHS.get(s[:3]) or SEASONS.get(s)


def iso(y, m=None, d=None):
    if not y:
        return ""
    return f"{y:04d}-{m or 1:02d}-{d or 1:02d}"


def date_of(el):
    """ISO date of a <Year><Month><Day> element (DateCreated, DateCompleted...)."""
    if el is None:
        return ""
    return iso(to_int(txt(el, "Year")), month_num(txt(el, "Month")), to_int(txt(el, "Day")))


def pub_date(article):
    """(year, month, day, raw) of the journal issue date.

    PubDate is either Year/Month/Day or a free MedlineDate ("2015 Nov-Dec",
    "2014-2015", "Spring 2016"): take the first year and month that appear.
    Falls back on ArticleDate (electronic publication) when the year is missing.
    """
    pd = article.find("Journal/JournalIssue/PubDate")
    raw = txt(pd) if pd is not None else ""
    y = m = d = None
    if pd is not None:
        y, m, d = to_int(txt(pd, "Year")), month_num(txt(pd, "Month")), to_int(txt(pd, "Day"))
        md = txt(pd, "MedlineDate")
        if md:
            yr = re.search(r"\b(1[89]\d\d|20\d\d)\b", md)
            mo = re.search(r"[A-Za-z]+", md)
            y = y or (int(yr.group(1)) if yr else None)
            m = m or (month_num(mo.group(0)) if mo else None)
    if y is None:
        ad = article.find("ArticleDate")
        if ad is not None:
            y, m, d = to_int(txt(ad, "Year")), month_num(txt(ad, "Month")), to_int(txt(ad, "Day"))
    return y, m, d, raw


def author_key(last, initials, collective):
    """Simple author identity used for deduplication in the warehouse."""
    if collective:
        return "collective:" + collective.lower()
    return f"{last.lower()}|{initials.lower()}" if last else ""


def extract(art, source, status, rows):
    mc = art.find("MedlineCitation")
    a = mc.find("Article")
    pmid = txt(mc, "PMID")
    y, m, d, raw = pub_date(a)
    journal = a.find("Journal")
    j = journal if journal is not None else ET.Element("Journal")
    info = mc.find("MedlineJournalInfo")
    info = info if info is not None else ET.Element("MedlineJournalInfo")
    abstract = " ".join(txt(t) for t in a.findall("Abstract/AbstractText"))
    doi = next((txt(e) for e in a.findall("ELocationID") if e.get("EIdType") == "doi"), "")
    if not doi:
        doi = next((txt(e) for e in art.findall("PubmedData/ArticleIdList/ArticleId")
                    if e.get("IdType") == "doi"), "")

    authors = a.findall("AuthorList/Author")
    for i, au in enumerate(authors, 1):
        last, ini, coll = txt(au, "LastName"), txt(au, "Initials"), txt(au, "CollectiveName")
        rows["authors"].append([pmid, i, int(i == 1), int(i == len(authors)), last,
                                txt(au, "ForeName"), ini, coll, author_key(last, ini, coll),
                                txt(au, "AffiliationInfo/Affiliation")])

    mesh = mc.findall("MeshHeadingList/MeshHeading")
    for mh in mesh:
        desc = mh.find("DescriptorName")
        if desc is None:  # damaged heading (partial record)
            continue
        quals = mh.findall("QualifierName")
        major = desc.get("MajorTopicYN") == "Y" or any(q.get("MajorTopicYN") == "Y" for q in quals)
        rows["mesh_headings"].append([pmid, desc.get("UI", ""), txt(desc), int(major),
                                      "|".join(txt(q) for q in quals)])

    keywords = mc.findall("KeywordList/Keyword")
    for kw in keywords:
        if txt(kw):
            rows["keywords"].append([pmid, txt(kw), int(kw.get("MajorTopicYN") == "Y")])

    for pt in a.findall("PublicationTypeList/PublicationType"):
        rows["publication_types"].append([pmid, pt.get("UI", ""), txt(pt)])

    for ch in mc.findall("ChemicalList/Chemical"):
        sub = ch.find("NameOfSubstance")
        rows["chemicals"].append([pmid, txt(ch, "RegistryNumber"),
                                  sub.get("UI", "") if sub is not None else "", txt(sub)])

    for g in a.findall("GrantList/Grant"):
        rows["grants"].append([pmid, txt(g, "GrantID"), txt(g, "Acronym"), txt(g, "Agency"),
                               txt(g, "Country")])

    rows["articles"].append([
        pmid, source, status, txt(a, "ArticleTitle"), "|".join(txt(l) for l in a.findall("Language")),
        y or "", m or "", d or "", iso(y, m, d), raw,
        date_of(mc.find("DateCreated")), date_of(mc.find("DateCompleted")),
        txt(info, "NlmUniqueID"), txt(j, "Title"), txt(j, "ISOAbbreviation"), txt(j, "ISSN"),
        txt(info, "ISSNLinking"), txt(info, "Country"),
        txt(j, "JournalIssue/Volume"), txt(j, "JournalIssue/Issue"),
        txt(a, "Pagination/MedlinePgn"), doi, a.get("PubModel", ""),
        txt(art, "PubmedData/PublicationStatus"), mc.get("Status", ""),
        len(authors), len(mesh), len(keywords),
        len(mc.findall("CommentsCorrectionsList/CommentsCorrections")),
        int(bool(abstract)), len(abstract)])


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    status_of = {}
    log = STAGING / "repair_log.csv"
    if log.exists():
        with open(log, encoding="utf-8") as fh:
            status_of = {r["file"]: r["status"] for r in csv.DictReader(fh)}

    rows = {name: [] for name in TABLES}
    seen = {}
    stats = Counter()
    files = sorted(CLEAN_DIR.glob("*.xml"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
    for path in files:
        root = ET.parse(path).getroot()
        for art in root:
            if art.tag != "PubmedArticle":
                stats[f"skipped_{art.tag}"] += 1
                continue
            pmid = txt(art, "MedlineCitation/PMID")
            if pmid in seen:
                rows["duplicates"].append([pmid, seen[pmid], path.name])
                stats["duplicate"] += 1
                continue
            seen[pmid] = path.name
            extract(art, path.name, status_of.get(path.name, ""), rows)
            stats["article"] += 1

    for name, header in TABLES.items():
        with open(OUT_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows[name])

    print(f"{len(files)} clean files read from {CLEAN_DIR}")
    for k, v in sorted(stats.items()):
        print(f"  {k:24s} {v}")
    for name in TABLES:
        print(f"  {name + '.csv':24s} {len(rows[name])} rows")
    print(f"tables -> {OUT_DIR}")


if __name__ == "__main__":
    main()

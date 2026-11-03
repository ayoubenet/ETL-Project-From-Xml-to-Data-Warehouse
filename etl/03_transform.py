"""
Step 3 - Transform the staging tables into warehouse-ready tables.

Reads staging/tables/*.csv (output of step 2), applies the conformance rules
and builds the star schema rows: surrogate keys, dimensions, fact and bridges.
Writes one CSV per warehouse table to staging/dw/, with the exact columns of
warehouse/schema.sql. No database is touched here (see step 4).

Conformance rules:
    language      lower-cased, first language kept as the article language
    journal       keyed on NLM unique ID, missing country -> 'Unknown'
    author        keyed on 'lastname|initials' (or collective name); the most
                  frequent fore name spelling is kept
    keyword       lower-cased and trimmed, duplicates per article removed
    pub. type     keyed on its name, case-insensitive (the UI is missing on
                  some records)
    attributes    missing values completed by later records of the same key
    dates         unknown month/day -> 1, precision kept on the fact

Usage: python etl/03_transform.py [staging_dir]
"""
import calendar
import csv
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "staging"
IN_DIR = STAGING / "tables"
OUT_DIR = STAGING / "dw"

LANGUAGES = {"eng": "English", "fre": "French", "chi": "Chinese", "ger": "German",
             "jpn": "Japanese", "spa": "Spanish", "rus": "Russian", "dan": "Danish",
             "hun": "Hungarian", "pol": "Polish", "dut": "Dutch", "cze": "Czech",
             "ita": "Italian", "nor": "Norwegian", "por": "Portuguese", "und": "Undetermined"}

# warehouse tables, in load order, with their columns (same order as schema.sql)
COLUMNS = {
    "dim_date": ["date_key", "full_date", "year", "semester", "quarter", "month", "month_name",
                 "day", "day_of_week", "day_name"],
    "dim_journal": ["journal_key", "nlm_id", "title", "iso_abbrev", "issn", "issn_linking",
                    "country"],
    "dim_language": ["language_key", "code", "name"],
    "dim_author": ["author_key", "author_code", "last_name", "fore_name", "initials",
                   "collective_name", "is_collective"],
    "dim_mesh": ["mesh_key", "descriptor_ui", "descriptor"],
    "dim_keyword": ["keyword_key", "keyword"],
    "dim_publication_type": ["pub_type_key", "name", "type_ui"],
    "dim_chemical": ["chemical_key", "substance", "substance_ui", "registry_number"],
    "dim_agency": ["agency_key", "agency", "country"],
    "fact_publication": ["pmid", "pub_date_key", "pub_date_precision", "created_date_key",
                         "journal_key", "language_key", "title", "doi", "volume", "issue",
                         "pages", "repair_status", "publication_count", "n_authors", "n_mesh",
                         "n_keywords", "n_chemicals", "n_grants", "n_references",
                         "has_abstract", "abstract_length"],
    "bridge_publication_author": ["pmid", "author_key", "position", "is_first", "is_last",
                                  "weight", "affiliation"],
    "bridge_publication_mesh": ["pmid", "mesh_key", "major_topic", "qualifiers"],
    "bridge_publication_keyword": ["pmid", "keyword_key"],
    "bridge_publication_type": ["pmid", "pub_type_key"],
    "bridge_publication_chemical": ["pmid", "chemical_key"],
    "bridge_publication_agency": ["pmid", "agency_key", "n_grants"],
}


def read(name):
    with open(IN_DIR / f"{name}.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Dim:
    """Surrogate key generator for one dimension: natural key -> (key, attributes).

    Attributes missing on the first occurrence ('' / None / 'Unknown') are
    completed by later occurrences of the same natural key.
    """

    def __init__(self):
        self.rows = {}

    def key(self, natural, *attrs):
        if natural not in self.rows:
            self.rows[natural] = (len(self.rows) + 1, attrs)
        else:
            k, old = self.rows[natural]
            merged = tuple(n if o in ("", None, "Unknown") and n not in ("", None) else o
                           for o, n in zip(old, attrs))
            self.rows[natural] = (k, merged)
        return self.rows[natural][0]

    def records(self):
        return [(k, nat, *attrs) for nat, (k, attrs) in self.rows.items()]


def date_key(d):
    return d.year * 10000 + d.month * 100 + d.day


def parse_iso(s):
    return date.fromisoformat(s) if s else None


def build_dim_date(dates):
    rows = []
    for d in sorted(dates):
        rows.append((date_key(d), d.isoformat(), d.year, 1 if d.month <= 6 else 2,
                     (d.month - 1) // 3 + 1, d.month, calendar.month_name[d.month], d.day,
                     d.isoweekday(), calendar.day_name[d.weekday()]))
    return rows


def transform():
    """Return {warehouse table: rows}."""
    articles = read("articles")
    authors, mesh, keywords = read("authors"), read("mesh_headings"), read("keywords")
    pub_types, chemicals, grants = read("publication_types"), read("chemicals"), read("grants")

    journals, languages, dim_authors = Dim(), Dim(), Dim()
    dim_mesh, dim_keywords, dim_types, dim_chems, dim_agencies = Dim(), Dim(), Dim(), Dim(), Dim()
    dates = set()
    n_chem = Counter(r["pmid"] for r in chemicals)
    n_grants = Counter(r["pmid"] for r in grants)

    # ---- fact
    facts = []
    for a in articles:
        pub = parse_iso(a["pub_date"])
        created = parse_iso(a["created_date"])
        dates.update(d for d in (pub, created) if d)
        precision = "day" if a["pub_day"] else "month" if a["pub_month"] else "year"
        jkey = None
        if a["journal_nlm_id"]:
            jkey = journals.key(a["journal_nlm_id"], a["journal_title"] or "Unknown",
                                a["journal_iso"], a["journal_issn"], a["journal_issn_linking"],
                                a["journal_country"] or "Unknown")
        code = (a["language"].split("|")[0] or "und").lower()
        lkey = languages.key(code, LANGUAGES.get(code, code))
        facts.append((int(a["pmid"]), date_key(pub), precision,
                      date_key(created) if created else None, jkey, lkey, a["title"],
                      a["doi"] or None, a["volume"] or None, a["issue"] or None,
                      a["pages"] or None, a["repair_status"] or "ok", 1,
                      int(a["n_authors"]), int(a["n_mesh"]), int(a["n_keywords"]),
                      n_chem[a["pmid"]], n_grants[a["pmid"]], int(a["n_references"]),
                      int(a["has_abstract"]), int(a["abstract_length"])))

    # ---- authors: keep the most frequent fore name per author code
    fore_names = defaultdict(Counter)
    for r in authors:
        if r["author_key"] and r["fore_name"]:
            fore_names[r["author_key"]][r["fore_name"]] += 1
    n_auth = Counter(r["pmid"] for r in authors if r["author_key"])
    b_authors = []
    for r in authors:
        code = r["author_key"]
        if not code:
            continue
        fore = fore_names[code].most_common(1)[0][0] if fore_names[code] else None
        coll = r["collective_name"] or None
        akey = dim_authors.key(code, r["last_name"] or None, fore, r["initials"] or None,
                               coll, int(coll is not None))
        b_authors.append((int(r["pmid"]), akey, int(r["position"]), int(r["is_first"]),
                          int(r["is_last"]), 1 / n_auth[r["pmid"]], r["affiliation"] or None))

    # ---- other bridges (deduplicated per article)
    b_mesh = {}
    for r in mesh:
        mkey = dim_mesh.key(r["descriptor_ui"] or r["descriptor"], r["descriptor"])
        prev = b_mesh.get((r["pmid"], mkey))
        major = max(int(r["major_topic"]), prev[2] if prev else 0)
        b_mesh[(r["pmid"], mkey)] = (int(r["pmid"]), mkey, major, r["qualifiers"] or None)

    b_keywords = set()
    for r in keywords:
        kw = " ".join(r["keyword"].lower().split())
        if kw:
            b_keywords.add((int(r["pmid"]), dim_keywords.key(kw)))

    # same type written in different cases ("JOURNAL ARTICLE"): keep the most
    # frequent spelling, preferring one that is not all upper case
    spellings = defaultdict(Counter)
    for r in pub_types:
        spellings[r["publication_type"].lower()][r["publication_type"]] += 1
    canonical = {low: max(c, key=lambda s: (not s.isupper(), c[s])) for low, c in spellings.items()}
    b_types = set()
    for r in pub_types:
        if r["publication_type"]:
            name = canonical[r["publication_type"].lower()]
            tkey = dim_types.key(name, r["type_ui"] or None)
            b_types.add((int(r["pmid"]), tkey))

    b_chems = set()
    for r in chemicals:
        if r["substance"]:
            ckey = dim_chems.key(r["substance"], r["substance_ui"] or None,
                                 r["registry_number"] or None)
            b_chems.add((int(r["pmid"]), ckey))

    agency_grants = Counter()
    for r in grants:
        if r["agency"]:
            gkey = dim_agencies.key(r["agency"], r["country"] or "Unknown")
            agency_grants[(int(r["pmid"]), gkey)] += 1

    return {
        "dim_date": build_dim_date(dates),
        "dim_journal": journals.records(),
        "dim_language": languages.records(),
        "dim_author": dim_authors.records(),
        "dim_mesh": dim_mesh.records(),
        "dim_keyword": dim_keywords.records(),
        "dim_publication_type": dim_types.records(),
        "dim_chemical": dim_chems.records(),
        "dim_agency": dim_agencies.records(),
        "fact_publication": facts,
        "bridge_publication_author": b_authors,
        "bridge_publication_mesh": list(b_mesh.values()),
        "bridge_publication_keyword": sorted(b_keywords),
        "bridge_publication_type": sorted(b_types),
        "bridge_publication_chemical": sorted(b_chems),
        "bridge_publication_agency": [(p, k, n) for (p, k), n in agency_grants.items()],
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tables = transform()
    for name, header in COLUMNS.items():
        with open(OUT_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(tables[name])  # None is written as an empty field

    print(f"staging tables read from {IN_DIR}")
    for name in COLUMNS:
        print(f"  {name + '.csv':34s} {len(tables[name])} rows")
    print(f"warehouse tables -> {OUT_DIR}")


if __name__ == "__main__":
    main()

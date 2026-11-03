"""
Step 4 - Load the transformed tables into the warehouse.

(Re)creates the database from warehouse/schema.sql and inserts every CSV of
staging/dw/ (output of step 3) into the table of the same name. No business
rule is applied here: empty fields become NULL, everything else is loaded as is.
The load is a full refresh: running it again rebuilds the same database.

Ends with reconciliation checks:
    - every CSV row is in its table
    - staging (step 2) counts match the warehouse
    - referential integrity of the fact and bridges

Usage: python etl/04_load.py [staging_dir] [db_path]
"""
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "staging"
DB_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "warehouse" / "pubmed_dw.db"
DW_DIR = STAGING / "dw"
SCHEMA = ROOT / "warehouse" / "schema.sql"

# dimensions before the fact, fact before the bridges (foreign keys)
LOAD_ORDER = ["dim_date", "dim_journal", "dim_language", "dim_author", "dim_mesh", "dim_keyword",
              "dim_publication_type", "dim_chemical", "dim_agency", "fact_publication",
              "bridge_publication_author", "bridge_publication_mesh",
              "bridge_publication_keyword", "bridge_publication_type",
              "bridge_publication_chemical", "bridge_publication_agency"]

# (child table, column, parent table, parent key)
FOREIGN_KEYS = [
    ("fact_publication", "pub_date_key", "dim_date", "date_key"),
    ("fact_publication", "created_date_key", "dim_date", "date_key"),
    ("fact_publication", "journal_key", "dim_journal", "journal_key"),
    ("fact_publication", "language_key", "dim_language", "language_key"),
    ("bridge_publication_author", "author_key", "dim_author", "author_key"),
    ("bridge_publication_mesh", "mesh_key", "dim_mesh", "mesh_key"),
    ("bridge_publication_keyword", "keyword_key", "dim_keyword", "keyword_key"),
    ("bridge_publication_type", "pub_type_key", "dim_publication_type", "pub_type_key"),
    ("bridge_publication_chemical", "chemical_key", "dim_chemical", "chemical_key"),
    ("bridge_publication_agency", "agency_key", "dim_agency", "agency_key"),
] + [(b, "pmid", "fact_publication", "pmid") for b in LOAD_ORDER if b.startswith("bridge_")]


def count_csv(path, keep=lambda r: True):
    with open(path, encoding="utf-8") as fh:
        return sum(1 for r in csv.DictReader(fh) if keep(r))


def load_table(con, name):
    with open(DW_DIR / f"{name}.csv", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [[v if v != "" else None for v in row] for row in reader]
    if rows:
        con.executemany(f"INSERT INTO {name} ({','.join(header)}) "
                        f"VALUES ({','.join('?' * len(header))})", rows)
    return len(rows)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    loaded = {name: load_table(con, name) for name in LOAD_ORDER}
    con.commit()

    q = lambda sql: con.execute(sql).fetchone()[0]
    checks = [(f"{t} rows", n, q(f"SELECT COUNT(*) FROM {t}")) for t, n in loaded.items()]
    tables = STAGING / "tables"
    checks += [
        ("staging articles", count_csv(tables / "articles.csv"),
         q("SELECT COUNT(*) FROM fact_publication")),
        ("staging author links", count_csv(tables / "authors.csv", lambda r: r["author_key"]),
         q("SELECT COUNT(*) FROM bridge_publication_author")),
        ("author weights sum to 1", q("SELECT COUNT(DISTINCT pmid) FROM bridge_publication_author"),
         q("SELECT COUNT(*) FROM (SELECT pmid FROM bridge_publication_author GROUP BY pmid "
           "HAVING ABS(SUM(weight) - 1) < 1e-9)")),
    ]
    for child, col, parent, key in FOREIGN_KEYS:
        checks.append((f"{child}.{col} -> {parent}", 0,
                       q(f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p "
                         f"ON p.{key} = c.{col} WHERE c.{col} IS NOT NULL AND p.{key} IS NULL")))

    print(f"warehouse -> {DB_PATH}")
    for t, n in loaded.items():
        print(f"  {t:30s} {n}")
    failed = 0
    for name, expected, got in checks:
        ok = expected == got
        failed += not ok
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: expected {expected}, got {got}")
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    con.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

# PubMed data warehouse

ETL pipeline from raw PubMed XML files (`Data/`) to a star-schema warehouse (SQLite).

```
python3 etl/01_repair.py    # Data/            -> staging/clean_xml/ + staging/repair_log.csv
python3 etl/02_extract.py   # staging/clean_xml -> staging/tables/*.csv
python3 etl/03_transform.py # staging/tables    -> staging/dw/*.csv (one file per warehouse table)
python3 etl/04_load.py      # staging/dw        -> warehouse/pubmed_dw.db (+ reconciliation checks)
sqlite3 -header -column warehouse/pubmed_dw.db < warehouse/queries.sql
```

| Step | E / T / L | Role |
|---|---|---|
| 01_repair | E + T | read raw XML, fix encoding and structure |
| 02_extract | E | flatten XML into staging tables (normalised values) |
| 03_transform | T | conformance rules, surrogate keys, dimensions, fact, bridges |
| 04_load | L | create schema, insert, reconciliation and integrity checks |

Each step reads only the output of the previous one and fully rebuilds its own
output, so any step can be rerun. Raw files are never modified.

## Schema (`warehouse/schema.sql`)

Fact `fact_publication`, one row per article (PMID): counts of authors, MeSH terms,
keywords, chemicals, grants and references, plus abstract presence and length.

| Dimension | Natural key | Link to fact |
|---|---|---|
| `dim_date` | yyyymmdd | `pub_date_key`, `created_date_key` |
| `dim_journal` | NLM unique ID | `journal_key` |
| `dim_language` | ISO 639-2 code | `language_key` |
| `dim_author` | `lastname\|initials` | `bridge_publication_author` (position, first/last, weight = 1/n_authors) |
| `dim_mesh` | descriptor UI | `bridge_publication_mesh` (major topic, qualifiers) |
| `dim_keyword` | lower-cased keyword | `bridge_publication_keyword` |
| `dim_publication_type` | type name | `bridge_publication_type` |
| `dim_chemical` | substance name | `bridge_publication_chemical` |
| `dim_agency` | agency name | `bridge_publication_agency` |

## Known limitations

- Authors are identified by last name + initials. Two people sharing them
  (frequent for names like "Wang Y") are merged.
- `pub_date_precision` tells whether the publication date is known to the day,
  month or year only. Missing parts are set to 1, so filter on precision for
  monthly or quarterly analysis.
- `repair_status` (`ok` / `repaired` / `partial`) comes from step 1. Partial
  records have fewer MeSH terms on average, so exclude them for quality-sensitive analyses.

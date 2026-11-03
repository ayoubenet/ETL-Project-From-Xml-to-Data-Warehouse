-- PubMed publications data warehouse - star schema.
--
-- Grain of the fact table: one row per published article (PMID).
-- Multi-valued dimensions (authors, MeSH terms, keywords, publication types,
-- chemicals, grant agencies) are linked through bridge tables.
--
-- Written in portable SQL (SQLite / PostgreSQL).

DROP TABLE IF EXISTS bridge_publication_agency;
DROP TABLE IF EXISTS bridge_publication_chemical;
DROP TABLE IF EXISTS bridge_publication_type;
DROP TABLE IF EXISTS bridge_publication_keyword;
DROP TABLE IF EXISTS bridge_publication_mesh;
DROP TABLE IF EXISTS bridge_publication_author;
DROP TABLE IF EXISTS fact_publication;
DROP TABLE IF EXISTS dim_agency;
DROP TABLE IF EXISTS dim_chemical;
DROP TABLE IF EXISTS dim_publication_type;
DROP TABLE IF EXISTS dim_keyword;
DROP TABLE IF EXISTS dim_mesh;
DROP TABLE IF EXISTS dim_author;
DROP TABLE IF EXISTS dim_language;
DROP TABLE IF EXISTS dim_journal;
DROP TABLE IF EXISTS dim_date;

-- ---------------------------------------------------------------- dimensions

CREATE TABLE dim_date (
    date_key      INTEGER PRIMARY KEY,          -- yyyymmdd
    full_date     DATE NOT NULL,
    year          INTEGER NOT NULL,
    semester      INTEGER NOT NULL,
    quarter       INTEGER NOT NULL,
    month         INTEGER NOT NULL,
    month_name    VARCHAR(10) NOT NULL,
    day           INTEGER NOT NULL,
    day_of_week   INTEGER NOT NULL,             -- 1 = Monday
    day_name      VARCHAR(10) NOT NULL
);

CREATE TABLE dim_journal (
    journal_key   INTEGER PRIMARY KEY,
    nlm_id        VARCHAR(20) NOT NULL UNIQUE,  -- natural key
    title         VARCHAR(500) NOT NULL,
    iso_abbrev    VARCHAR(200),
    issn          VARCHAR(20),
    issn_linking  VARCHAR(20),
    country       VARCHAR(100) NOT NULL         -- 'Unknown' when missing
);

CREATE TABLE dim_language (
    language_key  INTEGER PRIMARY KEY,
    code          VARCHAR(3) NOT NULL UNIQUE,   -- ISO 639-2 as given by PubMed
    name          VARCHAR(50) NOT NULL
);

CREATE TABLE dim_author (
    author_key      INTEGER PRIMARY KEY,
    author_code     VARCHAR(300) NOT NULL UNIQUE, -- 'lastname|initials' or 'collective:name'
    last_name       VARCHAR(200),
    fore_name       VARCHAR(200),               -- most frequent spelling
    initials        VARCHAR(20),
    collective_name VARCHAR(500),
    is_collective   INTEGER NOT NULL
);

CREATE TABLE dim_mesh (
    mesh_key      INTEGER PRIMARY KEY,
    descriptor_ui VARCHAR(20) NOT NULL UNIQUE,
    descriptor    VARCHAR(300) NOT NULL
);

CREATE TABLE dim_keyword (
    keyword_key   INTEGER PRIMARY KEY,
    keyword       VARCHAR(300) NOT NULL UNIQUE  -- lower-cased, trimmed
);

CREATE TABLE dim_publication_type (
    pub_type_key  INTEGER PRIMARY KEY,
    name          VARCHAR(200) NOT NULL UNIQUE,
    type_ui       VARCHAR(20)                   -- missing on some records
);

CREATE TABLE dim_chemical (
    chemical_key    INTEGER PRIMARY KEY,
    substance       VARCHAR(500) NOT NULL UNIQUE,
    substance_ui    VARCHAR(20),
    registry_number VARCHAR(50)
);

CREATE TABLE dim_agency (
    agency_key    INTEGER PRIMARY KEY,
    agency        VARCHAR(500) NOT NULL UNIQUE,
    country       VARCHAR(100) NOT NULL
);

-- ---------------------------------------------------------------- fact

CREATE TABLE fact_publication (
    pmid               INTEGER PRIMARY KEY,     -- degenerate dimension
    pub_date_key       INTEGER NOT NULL REFERENCES dim_date(date_key),
    pub_date_precision VARCHAR(5) NOT NULL,     -- 'day' | 'month' | 'year'
    created_date_key   INTEGER REFERENCES dim_date(date_key),
    journal_key        INTEGER REFERENCES dim_journal(journal_key),
    language_key       INTEGER REFERENCES dim_language(language_key),
    title              TEXT NOT NULL,
    doi                VARCHAR(200),
    volume             VARCHAR(50),
    issue              VARCHAR(50),
    pages              VARCHAR(50),
    repair_status      VARCHAR(10) NOT NULL,    -- data quality from step 1
    -- measures
    publication_count  INTEGER NOT NULL DEFAULT 1,
    n_authors          INTEGER NOT NULL,
    n_mesh             INTEGER NOT NULL,
    n_keywords         INTEGER NOT NULL,
    n_chemicals        INTEGER NOT NULL,
    n_grants           INTEGER NOT NULL,
    n_references       INTEGER NOT NULL,
    has_abstract       INTEGER NOT NULL,
    abstract_length    INTEGER NOT NULL
);

-- ---------------------------------------------------------------- bridges

CREATE TABLE bridge_publication_author (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    author_key    INTEGER NOT NULL REFERENCES dim_author(author_key),
    position      INTEGER NOT NULL,
    is_first      INTEGER NOT NULL,
    is_last       INTEGER NOT NULL,
    weight        REAL NOT NULL,                -- 1 / n_authors (fractional counting)
    affiliation   TEXT,
    PRIMARY KEY (pmid, position)
);

CREATE TABLE bridge_publication_mesh (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    mesh_key      INTEGER NOT NULL REFERENCES dim_mesh(mesh_key),
    major_topic   INTEGER NOT NULL,
    qualifiers    TEXT,
    PRIMARY KEY (pmid, mesh_key)
);

CREATE TABLE bridge_publication_keyword (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    keyword_key   INTEGER NOT NULL REFERENCES dim_keyword(keyword_key),
    PRIMARY KEY (pmid, keyword_key)
);

CREATE TABLE bridge_publication_type (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    pub_type_key  INTEGER NOT NULL REFERENCES dim_publication_type(pub_type_key),
    PRIMARY KEY (pmid, pub_type_key)
);

CREATE TABLE bridge_publication_chemical (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    chemical_key  INTEGER NOT NULL REFERENCES dim_chemical(chemical_key),
    PRIMARY KEY (pmid, chemical_key)
);

CREATE TABLE bridge_publication_agency (
    pmid          INTEGER NOT NULL REFERENCES fact_publication(pmid),
    agency_key    INTEGER NOT NULL REFERENCES dim_agency(agency_key),
    n_grants      INTEGER NOT NULL,
    PRIMARY KEY (pmid, agency_key)
);

CREATE INDEX ix_fact_date    ON fact_publication(pub_date_key);
CREATE INDEX ix_fact_journal ON fact_publication(journal_key);
CREATE INDEX ix_author_key   ON bridge_publication_author(author_key);
CREATE INDEX ix_mesh_key     ON bridge_publication_mesh(mesh_key);
CREATE INDEX ix_keyword_key  ON bridge_publication_keyword(keyword_key);

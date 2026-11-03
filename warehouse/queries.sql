-- Analytical queries on the PubMed warehouse.
-- Run: sqlite3 -header -column warehouse/pubmed_dw.db < warehouse/queries.sql

-- Q1. Publications per year and quarter (roll-up with yearly subtotal;
--     articles dated by year only appear in the subtotal)
SELECT year, COALESCE(CAST(quarter AS TEXT), 'Total') AS quarter, publications
FROM (
    SELECT d.year, d.quarter, SUM(f.publication_count) AS publications
    FROM fact_publication f JOIN dim_date d ON d.date_key = f.pub_date_key
    WHERE f.pub_date_precision <> 'year'
    GROUP BY d.year, d.quarter
    UNION ALL
    SELECT d.year, NULL, SUM(f.publication_count)
    FROM fact_publication f JOIN dim_date d ON d.date_key = f.pub_date_key
    GROUP BY d.year
)
ORDER BY year, quarter IS NULL, quarter;

-- Q2. Top 10 journals by number of articles, with their country
SELECT j.title, j.country, COUNT(*) AS publications,
       ROUND(AVG(f.n_authors), 1) AS avg_authors
FROM fact_publication f JOIN dim_journal j ON j.journal_key = f.journal_key
GROUP BY j.journal_key
ORDER BY publications DESC
LIMIT 10;

-- Q3. Publications by journal country and year (pivot)
SELECT j.country,
       SUM(d.year = 2013) AS y2013, SUM(d.year = 2014) AS y2014,
       SUM(d.year = 2015) AS y2015, SUM(d.year = 2016) AS y2016,
       COUNT(*) AS total
FROM fact_publication f
JOIN dim_journal j ON j.journal_key = f.journal_key
JOIN dim_date d ON d.date_key = f.pub_date_key
GROUP BY j.country
ORDER BY total DESC
LIMIT 10;

-- Q4. Most productive authors: full count vs fractional count
SELECT a.last_name, a.fore_name,
       COUNT(*) AS articles,
       ROUND(SUM(b.weight), 2) AS fractional_articles,
       SUM(b.is_first) AS as_first_author,
       SUM(b.is_last) AS as_last_author
FROM bridge_publication_author b JOIN dim_author a ON a.author_key = b.author_key
WHERE a.is_collective = 0
GROUP BY a.author_key
ORDER BY articles DESC
LIMIT 10;

-- Q5. Top 15 MeSH descriptors as major topic
SELECT m.descriptor, COUNT(*) AS articles
FROM bridge_publication_mesh b JOIN dim_mesh m ON m.mesh_key = b.mesh_key
WHERE b.major_topic = 1
GROUP BY m.mesh_key
ORDER BY articles DESC
LIMIT 15;

-- Q6. Trend of the 5 most frequent author keywords per year
WITH top_kw AS (
    SELECT keyword_key FROM bridge_publication_keyword
    GROUP BY keyword_key ORDER BY COUNT(*) DESC LIMIT 5
)
SELECT k.keyword, d.year, COUNT(*) AS articles
FROM bridge_publication_keyword b
JOIN top_kw t ON t.keyword_key = b.keyword_key
JOIN dim_keyword k ON k.keyword_key = b.keyword_key
JOIN fact_publication f ON f.pmid = b.pmid
JOIN dim_date d ON d.date_key = f.pub_date_key
GROUP BY k.keyword, d.year
ORDER BY k.keyword, d.year;

-- Q7. Publication types: volume and average reference count
SELECT t.name AS publication_type, COUNT(*) AS articles,
       ROUND(AVG(f.n_references), 1) AS avg_references,
       ROUND(100.0 * AVG(f.has_abstract), 1) AS pct_with_abstract
FROM bridge_publication_type b
JOIN dim_publication_type t ON t.pub_type_key = b.pub_type_key
JOIN fact_publication f ON f.pmid = b.pmid
GROUP BY t.pub_type_key
ORDER BY articles DESC
LIMIT 10;

-- Q8. Funding: top grant agencies and the share of funded articles per year
SELECT g.agency, g.country, COUNT(DISTINCT b.pmid) AS funded_articles,
       SUM(b.n_grants) AS grants
FROM bridge_publication_agency b JOIN dim_agency g ON g.agency_key = b.agency_key
GROUP BY g.agency_key
ORDER BY funded_articles DESC
LIMIT 10;

SELECT d.year, COUNT(*) AS publications,
       SUM(f.n_grants > 0) AS funded,
       ROUND(100.0 * SUM(f.n_grants > 0) / COUNT(*), 1) AS pct_funded
FROM fact_publication f JOIN dim_date d ON d.date_key = f.pub_date_key
GROUP BY d.year ORDER BY d.year;

-- Q9. Language distribution
SELECT l.name AS language, COUNT(*) AS publications,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM fact_publication), 2) AS pct
FROM fact_publication f JOIN dim_language l ON l.language_key = f.language_key
GROUP BY l.language_key ORDER BY publications DESC;

-- Q10. Data quality: articles per repair status (from step 1)
SELECT repair_status, COUNT(*) AS publications,
       ROUND(AVG(n_authors), 1) AS avg_authors, ROUND(AVG(n_mesh), 1) AS avg_mesh
FROM fact_publication GROUP BY repair_status ORDER BY publications DESC;

CREATE OR REPLACE TABLE nist_names AS
SELECT DISTINCT lower(trim(name)) AS norm_name
FROM read_json_auto('NISTds.jsonl', format='newline_delimited');
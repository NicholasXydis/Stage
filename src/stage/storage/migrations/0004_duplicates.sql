ALTER TABLE jobs ADD COLUMN duplicate_of TEXT;

CREATE INDEX idx_jobs_duplicate_of ON jobs (duplicate_of);
CREATE INDEX idx_jobs_company_canonical ON jobs (company) WHERE duplicate_of IS NULL;

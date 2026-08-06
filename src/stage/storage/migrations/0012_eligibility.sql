ALTER TABLE jobs ADD COLUMN degree_requirement TEXT NOT NULL DEFAULT 'unknown';

CREATE INDEX idx_jobs_degree ON jobs (degree_requirement);

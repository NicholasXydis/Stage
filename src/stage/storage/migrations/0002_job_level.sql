ALTER TABLE jobs ADD COLUMN level TEXT NOT NULL DEFAULT 'internship';

CREATE INDEX idx_jobs_level ON jobs (level);

ALTER TABLE quarantine ADD COLUMN employment_type TEXT NOT NULL DEFAULT '';

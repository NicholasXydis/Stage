CREATE TABLE quarantine (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    company         TEXT NOT NULL,
    title_raw       TEXT NOT NULL,
    apply_url_raw   TEXT NOT NULL DEFAULT '',
    location_raw    TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT 'unknown',
    remote_scope    TEXT,
    reason          TEXT NOT NULL,
    matched_phrase  TEXT NOT NULL DEFAULT '',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX idx_quarantine_reason_first_seen ON quarantine (reason, first_seen DESC);
CREATE INDEX idx_quarantine_first_seen ON quarantine (first_seen DESC);
CREATE INDEX idx_quarantine_source_company ON quarantine (source, company);

ALTER TABLE sync_run_sources ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0;

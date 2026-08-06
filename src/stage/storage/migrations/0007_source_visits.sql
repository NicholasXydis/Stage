CREATE TABLE source_visits (
    source               TEXT NOT NULL,
    company              TEXT NOT NULL,
    last_attempt_at      TEXT NOT NULL,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, company)
);

CREATE INDEX idx_source_visits_success ON source_visits (source, last_success_at);

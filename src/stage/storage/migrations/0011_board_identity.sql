CREATE TABLE source_visits_rebuilt (
    source               TEXT NOT NULL,
    board                TEXT NOT NULL,
    label                TEXT NOT NULL DEFAULT '',
    last_attempt_at      TEXT NOT NULL,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, board)
);

DROP INDEX IF EXISTS idx_source_visits_success;

DROP TABLE source_visits;

ALTER TABLE source_visits_rebuilt RENAME TO source_visits;

CREATE INDEX idx_source_visits_success ON source_visits (source, last_success_at);

ALTER TABLE detail_fetches ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;

ALTER TABLE detail_fetches ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;

DROP INDEX IF EXISTS idx_detail_fetches_source;

CREATE INDEX idx_detail_fetches_source ON detail_fetches (source, resolved, failed);

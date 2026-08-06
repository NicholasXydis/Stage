CREATE TABLE detail_fetches (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_detail_fetches_source ON detail_fetches (source, resolved);

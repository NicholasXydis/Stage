CREATE TABLE http_cache (
    url           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX idx_http_cache_source ON http_cache (source);

ALTER TABLE sync_run_sources ADD COLUMN not_modified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_run_sources ADD COLUMN retries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_run_sources ADD COLUMN tightenings INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_run_sources ADD COLUMN latency_p50_ms REAL NOT NULL DEFAULT 0;
ALTER TABLE sync_run_sources ADD COLUMN latency_p95_ms REAL NOT NULL DEFAULT 0;

CREATE TABLE jobs (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    company             TEXT NOT NULL,
    title_raw           TEXT NOT NULL,
    title_normalized    TEXT NOT NULL,
    title_canonical     TEXT NOT NULL DEFAULT '',
    apply_url_raw       TEXT NOT NULL,
    apply_url_canonical TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    location_raw        TEXT NOT NULL DEFAULT '',
    location            TEXT NOT NULL DEFAULT 'unknown',
    remote_scope        TEXT,
    language            TEXT NOT NULL DEFAULT 'unknown',
    term                TEXT NOT NULL DEFAULT 'unknown',
    role                TEXT NOT NULL DEFAULT 'unknown',
    work_auth_flag      INTEGER NOT NULL DEFAULT 0,
    compensation        TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    source_posted_at    TEXT
);

CREATE INDEX idx_jobs_first_seen ON jobs (first_seen DESC);
CREATE INDEX idx_jobs_status_first_seen ON jobs (status, first_seen DESC);
CREATE INDEX idx_jobs_source_company ON jobs (source, company);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL
);

CREATE INDEX idx_sync_runs_started_at ON sync_runs (started_at DESC);

CREATE TABLE sync_run_sources (
    run_id     INTEGER NOT NULL REFERENCES sync_runs (id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    added      INTEGER NOT NULL DEFAULT 0,
    updated    INTEGER NOT NULL DEFAULT 0,
    closed     INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    requests   INTEGER NOT NULL DEFAULT 0,
    elapsed_ms REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source)
);

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
    degree_requirement  TEXT NOT NULL DEFAULT 'unknown',
    work_auth_flag      INTEGER NOT NULL DEFAULT 0,
    compensation        TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    duplicate_of        TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    source_posted_at    TEXT
);

CREATE INDEX idx_jobs_first_seen ON jobs (first_seen DESC);
CREATE INDEX idx_jobs_status_first_seen ON jobs (status, first_seen DESC);
CREATE INDEX idx_jobs_source_company ON jobs (source, company);
CREATE INDEX idx_jobs_duplicate_of ON jobs (duplicate_of);
CREATE INDEX idx_jobs_degree ON jobs (degree_requirement);
CREATE INDEX idx_jobs_company_canonical ON jobs (company) WHERE duplicate_of IS NULL;

CREATE VIRTUAL TABLE jobs_fts USING fts5 (
    company,
    title_raw,
    title_normalized,
    title_canonical,
    location_raw,
    description,
    content = 'jobs',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER jobs_fts_after_insert AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts (
        rowid, company, title_raw, title_normalized, title_canonical,
        location_raw, description
    )
    VALUES (
        new.rowid, new.company, new.title_raw, new.title_normalized,
        new.title_canonical, new.location_raw, new.description
    );
END;

CREATE TRIGGER jobs_fts_after_delete AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts (
        jobs_fts, rowid, company, title_raw, title_normalized, title_canonical,
        location_raw, description
    )
    VALUES (
        'delete', old.rowid, old.company, old.title_raw, old.title_normalized,
        old.title_canonical, old.location_raw, old.description
    );
END;

CREATE TRIGGER jobs_fts_after_update AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts (
        jobs_fts, rowid, company, title_raw, title_normalized, title_canonical,
        location_raw, description
    )
    VALUES (
        'delete', old.rowid, old.company, old.title_raw, old.title_normalized,
        old.title_canonical, old.location_raw, old.description
    );
    INSERT INTO jobs_fts (
        rowid, company, title_raw, title_normalized, title_canonical,
        location_raw, description
    )
    VALUES (
        new.rowid, new.company, new.title_raw, new.title_normalized,
        new.title_canonical, new.location_raw, new.description
    );
END;

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

CREATE INDEX idx_quarantine_first_seen ON quarantine (first_seen DESC);
CREATE INDEX idx_quarantine_reason_first_seen ON quarantine (reason, first_seen DESC);
CREATE INDEX idx_quarantine_source_company ON quarantine (source, company);

CREATE TABLE tombstones (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    purged_at  TEXT NOT NULL
);

CREATE INDEX idx_tombstones_purged_at ON tombstones (purged_at DESC);

CREATE TABLE http_cache (
    url           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX idx_http_cache_source ON http_cache (source);

CREATE TABLE rate_state (
    bucket                TEXT PRIMARY KEY,
    blocked_until         TEXT,
    min_interval_override REAL,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_failure_at       TEXT,
    reason                TEXT NOT NULL DEFAULT '',
    rotation_cursor       TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);

CREATE TABLE source_visits (
    source               TEXT NOT NULL,
    board                TEXT NOT NULL,
    label                TEXT NOT NULL DEFAULT '',
    last_attempt_at      TEXT NOT NULL,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, board)
);

CREATE INDEX idx_source_visits_success ON source_visits (source, last_success_at);

CREATE TABLE workday_facets (
    tenant       TEXT NOT NULL,
    site         TEXT NOT NULL,
    parameter    TEXT NOT NULL,
    facet_id     TEXT NOT NULL,
    descriptor   TEXT NOT NULL DEFAULT '',
    resolved_at  TEXT NOT NULL,
    PRIMARY KEY (tenant, site)
);

CREATE TABLE detail_fetches (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 1,
    failed     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_detail_fetches_source ON detail_fetches (source, resolved, failed);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL
);

CREATE INDEX idx_sync_runs_started_at ON sync_runs (started_at DESC);

CREATE TABLE sync_run_sources (
    run_id         INTEGER NOT NULL REFERENCES sync_runs (id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    fetched        INTEGER NOT NULL DEFAULT 0,
    added          INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    closed         INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    not_modified   INTEGER NOT NULL DEFAULT 0,
    retries        INTEGER NOT NULL DEFAULT 0,
    tightenings    INTEGER NOT NULL DEFAULT 0,
    quarantined    INTEGER NOT NULL DEFAULT 0,
    deferred       INTEGER NOT NULL DEFAULT 0,
    blocked        INTEGER NOT NULL DEFAULT 0,
    stored         INTEGER NOT NULL DEFAULT -1,
    latency_p50_ms REAL NOT NULL DEFAULT 0,
    latency_p95_ms REAL NOT NULL DEFAULT 0,
    elapsed_ms     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source)
);

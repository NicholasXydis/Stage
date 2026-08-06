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

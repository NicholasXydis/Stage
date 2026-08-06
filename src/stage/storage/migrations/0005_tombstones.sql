CREATE TABLE tombstones (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    purged_at  TEXT NOT NULL
);

CREATE INDEX idx_tombstones_purged_at ON tombstones (purged_at DESC);

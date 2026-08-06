CREATE TABLE workday_facets (
    tenant       TEXT NOT NULL,
    site         TEXT NOT NULL,
    parameter    TEXT NOT NULL,
    facet_id     TEXT NOT NULL,
    descriptor   TEXT NOT NULL DEFAULT '',
    resolved_at  TEXT NOT NULL,
    PRIMARY KEY (tenant, site)
);

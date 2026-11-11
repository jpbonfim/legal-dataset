-- Progress state of the pipeline. Every stage is resumable from this database.

CREATE TABLE IF NOT EXISTS norms (
    norm_id         TEXT PRIMARY KEY,           -- LexML URN
    kind            TEXT NOT NULL,
    number          TEXT NOT NULL,
    signed_on       TEXT NOT NULL,              -- ISO date
    sphere          TEXT NOT NULL,
    uf              TEXT,
    municipality    TEXT,
    subset          TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    catalog_source  TEXT NOT NULL,
    catalog_ref     TEXT NOT NULL,
    extra_json      TEXT NOT NULL DEFAULT '{}',
    legal_force     TEXT NOT NULL DEFAULT 'unknown',
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_norms_kind_number ON norms (kind, number);

CREATE TABLE IF NOT EXISTS text_sources (
    norm_id         TEXT PRIMARY KEY REFERENCES norms (norm_id),
    source_name     TEXT NOT NULL,
    url             TEXT NOT NULL,
    text_version    TEXT NOT NULL,
    raw_path        TEXT,
    etag            TEXT,
    last_modified   TEXT,
    fetched_at      TEXT
);

CREATE TABLE IF NOT EXISTS stage_status (
    norm_id         TEXT NOT NULL REFERENCES norms (norm_id),
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,              -- pending | done | failed | skipped
    attempts        INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,                       -- skip reason or error kind
    message         TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (norm_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_stage_status ON stage_status (stage, status);

CREATE TABLE IF NOT EXISTS discovery_pages (
    source          TEXT NOT NULL,
    partition_key   TEXT NOT NULL,              -- e.g. "lei/1990"
    status          TEXT NOT NULL,              -- done | failed
    item_count      INTEGER NOT NULL DEFAULT 0,
    message         TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (source, partition_key)
);

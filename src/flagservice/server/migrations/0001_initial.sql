-- Initial schema.
--
-- Keys are the public identifiers; integer ids stay internal. Flag variants,
-- rules and audit snapshots are JSON documents validated by the application
-- before they are written.

CREATE TABLE projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE environments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id   TEXT NOT NULL UNIQUE,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    key         TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (project_id, key)
);

CREATE TABLE api_keys (
    id              TEXT PRIMARY KEY,
    environment_id  INTEGER NOT NULL REFERENCES environments (id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('server', 'sdk')),
    name            TEXT NOT NULL,
    token_sha256    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT
);

CREATE INDEX api_keys_environment ON api_keys (environment_id);

-- AUTOINCREMENT keeps ids from being reused after a delete, because flag ETags
-- include the id: a recreated flag must never accept an ETag of the old one.
CREATE TABLE flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    key         TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('boolean', 'string', 'number', 'json')),
    description TEXT NOT NULL,
    salt        TEXT NOT NULL,
    variants    TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (project_id, key)
);

CREATE TABLE flag_settings (
    flag_id         INTEGER NOT NULL REFERENCES flags (id) ON DELETE CASCADE,
    environment_id  INTEGER NOT NULL REFERENCES environments (id) ON DELETE CASCADE,
    enabled         INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    default_variant TEXT NOT NULL,
    rules           TEXT NOT NULL,
    PRIMARY KEY (flag_id, environment_id)
);

CREATE INDEX flag_settings_environment ON flag_settings (environment_id);

-- No foreign key to projects: the history must outlive what it describes.
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL,
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    flag_key        TEXT,
    environment_key TEXT,
    before          TEXT,
    after           TEXT
);

CREATE INDEX audit_log_project ON audit_log (project_id, id);
CREATE INDEX audit_log_flag ON audit_log (project_id, flag_key, id);

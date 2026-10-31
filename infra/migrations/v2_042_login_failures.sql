-- v2_042 (SEC-02, 2026-10-30): the shared failed-login table owned by the schema (it was created on first use by the web process; the login
-- request no longer issues DDL when RIPPLE_LOGIN_SCHEMA_AUTO=0). Additive and idempotent: a database that already holds the table keeps its rows
-- and gains `locked_until` (the lock state of a key, independent of the counting window). Old code reading this table ignores the column.
CREATE TABLE IF NOT EXISTS v2_login_failures (
    key TEXT NOT NULL,
    window_start BIGINT NOT NULL,
    failures INTEGER NOT NULL,
    locked_until BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);
ALTER TABLE v2_login_failures ADD COLUMN IF NOT EXISTS locked_until BIGINT NOT NULL DEFAULT 0;
-- The shared request rate-limit windows of the same guard (created on first use by the web process before 2026-10-30; owned by the schema now).
CREATE TABLE IF NOT EXISTS v2_rate_windows (
    key TEXT NOT NULL,
    window_start BIGINT NOT NULL,
    hits INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);

CREATE TABLE IF NOT EXISTS platform_key_rate (
scope TEXT NOT NULL,
connector TEXT NOT NULL,
window_start TIMESTAMPTZ NOT NULL,
calls INTEGER NOT NULL DEFAULT 0,
PRIMARY KEY (scope, connector)
);

CREATE TABLE IF NOT EXISTS platform_instructions (
key TEXT PRIMARY KEY,
body_md TEXT NOT NULL DEFAULT '',
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_by TEXT
);

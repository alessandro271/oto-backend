CREATE TABLE IF NOT EXISTS users (
sub TEXT PRIMARY KEY,
email TEXT,
name TEXT,
role TEXT NOT NULL DEFAULT 'member',
avatar_url TEXT,
locale TEXT,
suspended_at TIMESTAMPTZ,
suspended_by TEXT,
suspended_reason TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

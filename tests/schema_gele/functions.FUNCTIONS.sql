CREATE TABLE IF NOT EXISTS functions (
id BIGSERIAL PRIMARY KEY,
owner_type TEXT NOT NULL,
owner_id TEXT NOT NULL,
slug TEXT NOT NULL,
title TEXT NOT NULL,
description TEXT NOT NULL DEFAULT '',
published_version INTEGER,
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (owner_type, owner_id, slug)
);
CREATE TABLE IF NOT EXISTS function_versions (
id BIGSERIAL PRIMARY KEY,
function_id BIGINT NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
version INTEGER NOT NULL,
status TEXT NOT NULL DEFAULT 'proposee'
CHECK (status IN ('proposee', 'publiee', 'refusee')),
sources JSONB NOT NULL,
entrypoint TEXT NOT NULL,
requirements TEXT[] NOT NULL DEFAULT '{}',
note TEXT,
proposed_by TEXT,
proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
decided_by TEXT,
decided_at TIMESTAMPTZ,
test_report JSONB,
UNIQUE (function_id, version)
);

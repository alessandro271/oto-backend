CREATE TABLE IF NOT EXISTS option_comps (
entity_type TEXT NOT NULL,
entity_id TEXT NOT NULL,
option TEXT NOT NULL,
granted_by TEXT,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ,
PRIMARY KEY (entity_type, entity_id, option)
);

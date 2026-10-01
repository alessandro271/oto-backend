CREATE TABLE IF NOT EXISTS connector_acl (
scope_type TEXT NOT NULL CHECK (scope_type IN ('org', 'group')),
scope_id TEXT NOT NULL,
connector TEXT NOT NULL,
principal_type TEXT NOT NULL CHECK (principal_type IN ('group', 'user')),
principal_id TEXT NOT NULL,
granted_by TEXT,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (scope_type, scope_id, connector, principal_type, principal_id)
);

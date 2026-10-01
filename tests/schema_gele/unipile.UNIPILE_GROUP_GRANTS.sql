CREATE TABLE IF NOT EXISTS connector_account_group_grants (
owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
provider TEXT NOT NULL,
account_id TEXT NOT NULL,
grantee_group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
granted_by TEXT NOT NULL,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (owner_sub, provider, grantee_group_id)
);
CREATE INDEX IF NOT EXISTS idx_account_group_grants_group
ON connector_account_group_grants(grantee_group_id, provider);

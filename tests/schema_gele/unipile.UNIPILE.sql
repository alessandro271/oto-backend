CREATE TABLE IF NOT EXISTS unipile_accounts (
sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
provider TEXT NOT NULL DEFAULT 'LINKEDIN',
account_id TEXT NOT NULL,
account_name TEXT,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
platform_seat BOOLEAN NOT NULL DEFAULT FALSE,
connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
disconnected_at TIMESTAMPTZ,
PRIMARY KEY (sub, org_id, provider)
);
CREATE TABLE IF NOT EXISTS unipile_pending (
nonce TEXT PRIMARY KEY,
sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
org_id BIGINT,
provider TEXT NOT NULL DEFAULT 'LINKEDIN',
platform_seat BOOLEAN NOT NULL DEFAULT FALSE,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS connector_account_grants (
owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
provider TEXT NOT NULL,
account_id TEXT NOT NULL,
grantee_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
granted_by TEXT NOT NULL,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (owner_sub, provider, grantee_sub)
);
CREATE INDEX IF NOT EXISTS idx_account_grants_grantee
ON connector_account_grants(grantee_sub, provider);
CREATE TABLE IF NOT EXISTS unipile_operated_accounts (
sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
provider TEXT NOT NULL,
account_id TEXT NOT NULL,
owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (sub, provider)
);

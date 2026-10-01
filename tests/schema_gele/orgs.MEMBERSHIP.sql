CREATE TABLE IF NOT EXISTS org_members (
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
org_role TEXT NOT NULL DEFAULT 'org_member',
is_active BOOLEAN NOT NULL DEFAULT FALSE,
joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (org_id, sub)
);
CREATE INDEX IF NOT EXISTS idx_org_members_sub ON org_members(sub);
CREATE UNIQUE INDEX IF NOT EXISTS org_members_one_active ON org_members(sub) WHERE is_active;
CREATE TABLE IF NOT EXISTS org_invitations (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
email TEXT,
org_role TEXT NOT NULL DEFAULT 'org_member',
token_hash TEXT NOT NULL UNIQUE,
code TEXT,
invited_by TEXT,
source TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ NOT NULL,
accepted_at TIMESTAMPTZ,
accepted_sub TEXT,
declined_at TIMESTAMPTZ,
declined_sub TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_invitations_org ON org_invitations(org_id);

CREATE TABLE IF NOT EXISTS org_entitlements (
org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
sub TEXT,
right_key TEXT NOT NULL,
value INTEGER NOT NULL,
source TEXT NOT NULL,
starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ,
granted_by TEXT,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT org_entitlements_une_ligne
UNIQUE NULLS NOT DISTINCT (org_id, sub, right_key, source),
CONSTRAINT org_entitlements_une_portee
CHECK (org_id IS NOT NULL OR sub IS NOT NULL)
);

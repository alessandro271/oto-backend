ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_type TEXT;
ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_kind TEXT;
ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_id TEXT;
ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_role TEXT;
ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_ttl_days INTEGER;
ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS resource_name TEXT;

CREATE TABLE IF NOT EXISTS org_groups (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
name TEXT NOT NULL,
description TEXT NOT NULL DEFAULT '',
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (org_id, name)
);
CREATE INDEX IF NOT EXISTS idx_org_groups_org ON org_groups(org_id);
CREATE TABLE IF NOT EXISTS org_group_members (
group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
group_role TEXT NOT NULL DEFAULT 'group_member',
is_active BOOLEAN NOT NULL DEFAULT FALSE,
joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (group_id, sub)
);
CREATE INDEX IF NOT EXISTS idx_org_group_members_sub ON org_group_members(sub);
CREATE UNIQUE INDEX IF NOT EXISTS org_group_members_one_active
ON org_group_members(sub) WHERE is_active;

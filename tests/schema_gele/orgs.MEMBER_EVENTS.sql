CREATE TABLE IF NOT EXISTS org_member_events (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
action TEXT NOT NULL CHECK (action IN ('added', 'removed', 'role_changed')),
old_role TEXT,
new_role TEXT,
actor_sub TEXT,
at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_org_member_events_org ON org_member_events(org_id, id DESC);

CREATE TABLE IF NOT EXISTS credential_disparitions (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
connector TEXT NOT NULL,
account TEXT NOT NULL DEFAULT '',
acteur_sub TEXT,
agents_count INTEGER NOT NULL DEFAULT 0,
agents JSONB NOT NULL DEFAULT '[]'::jsonb,
notifie_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cred_disparitions_a_notifier
ON credential_disparitions (org_id, created_at) WHERE notifie_at IS NULL;

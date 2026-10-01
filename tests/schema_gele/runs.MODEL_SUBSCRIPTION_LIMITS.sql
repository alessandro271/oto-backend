CREATE TABLE IF NOT EXISTS org_model_subscription_limits (
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
famille TEXT NOT NULL,
limite_pct SMALLINT NOT NULL CHECK (limite_pct BETWEEN 1 AND 100),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_by TEXT,
PRIMARY KEY (org_id, famille)
);

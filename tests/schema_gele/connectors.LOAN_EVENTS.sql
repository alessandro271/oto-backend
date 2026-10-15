CREATE TABLE IF NOT EXISTS connector_loan_events (
id BIGSERIAL PRIMARY KEY,
at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
actor_sub TEXT NOT NULL,
org_id BIGINT NOT NULL,
entity_type TEXT NOT NULL,
entity_id TEXT NOT NULL,
connector TEXT NOT NULL,
account TEXT NOT NULL DEFAULT '',
target TEXT NOT NULL,
action TEXT NOT NULL CHECK (action IN ('lend', 'revoke'))
);
CREATE INDEX IF NOT EXISTS idx_loan_events_actor ON connector_loan_events (actor_sub);
CREATE INDEX IF NOT EXISTS idx_loan_events_ligne
ON connector_loan_events (entity_type, entity_id, connector, account);

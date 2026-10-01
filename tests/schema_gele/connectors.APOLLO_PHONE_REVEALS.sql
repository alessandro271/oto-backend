CREATE TABLE IF NOT EXISTS apollo_phone_reveals (
token_hash TEXT PRIMARY KEY,
org_id BIGINT,
sub TEXT NOT NULL,
cle_portee TEXT NOT NULL,
request_id TEXT,
payload JSONB,
deliveries INTEGER NOT NULL DEFAULT 0,
received_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'
);
CREATE INDEX IF NOT EXISTS idx_apollo_phone_reveals_request
ON apollo_phone_reveals(cle_portee, request_id)
WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_apollo_phone_reveals_expires
ON apollo_phone_reveals(expires_at);

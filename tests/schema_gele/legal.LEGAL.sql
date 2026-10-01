CREATE TABLE IF NOT EXISTS legal_acceptances (
sub TEXT NOT NULL,
doc_slug TEXT NOT NULL,
version TEXT NOT NULL,
accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (sub, doc_slug)
);
CREATE TABLE IF NOT EXISTS legal_acceptance_events (
id BIGSERIAL PRIMARY KEY,
sub TEXT NOT NULL,
org_id BIGINT,
doc_slug TEXT NOT NULL,
version TEXT NOT NULL,
context TEXT,
ip TEXT,
user_agent TEXT,
accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_legal_events_dernier
ON legal_acceptance_events (sub, doc_slug, accepted_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS tenant_legal_docs (
tenant_slug TEXT NOT NULL,
doc_slug TEXT NOT NULL,
version TEXT NOT NULL,
label TEXT NOT NULL,
url TEXT NOT NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (tenant_slug, doc_slug)
);

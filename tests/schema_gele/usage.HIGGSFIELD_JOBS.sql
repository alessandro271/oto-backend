CREATE TABLE IF NOT EXISTS higgsfield_jobs (
request_id TEXT PRIMARY KEY,
org_id BIGINT,
sub TEXT,
run_id TEXT,
model TEXT NOT NULL,
prix_microdollars BIGINT NOT NULL,
approximatif BOOLEAN NOT NULL DEFAULT FALSE,
soumis_le TIMESTAMPTZ NOT NULL DEFAULT NOW(),
releve_le TIMESTAMPTZ,
statut TEXT
);
CREATE INDEX IF NOT EXISTS idx_higgsfield_jobs_a_relever
ON higgsfield_jobs(soumis_le) WHERE releve_le IS NULL;
CREATE INDEX IF NOT EXISTS idx_higgsfield_jobs_sub ON higgsfield_jobs(sub);

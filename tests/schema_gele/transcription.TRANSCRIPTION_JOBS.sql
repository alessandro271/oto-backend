CREATE TABLE IF NOT EXISTS transcription_jobs (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'pending',
audio_key TEXT NOT NULL,
filename TEXT NOT NULL,
mime TEXT,
language TEXT,
vocabulary TEXT,
api_key_enc TEXT NOT NULL,
page_id BIGINT,
result JSONB,
transcript JSONB,
error TEXT,
attempts INTEGER NOT NULL DEFAULT 0,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_pending
ON transcription_jobs(created_at) WHERE status = 'pending';

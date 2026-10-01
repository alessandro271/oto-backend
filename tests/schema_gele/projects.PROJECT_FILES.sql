CREATE INDEX IF NOT EXISTS idx_doc_change_requests_doc ON doc_change_requests(doc_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS project_activity (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
sub TEXT,
action TEXT NOT NULL,
detail TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_activity_project ON project_activity(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS project_files (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
s3_key TEXT NOT NULL,
filename TEXT NOT NULL,
mime TEXT,
size_bytes BIGINT,
title TEXT,
description TEXT,
summary TEXT,
public BOOLEAN NOT NULL DEFAULT FALSE,
public_url TEXT,
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);
CREATE TABLE IF NOT EXISTS project_file_texts (
file_id BIGINT PRIMARY KEY REFERENCES project_files(id) ON DELETE CASCADE,
status TEXT NOT NULL,
extracted_text TEXT NOT NULL DEFAULT '',
pages INTEGER,
detail TEXT NOT NULL DEFAULT '',
attempts INTEGER NOT NULL DEFAULT 1,
extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_file_texts_retry
ON project_file_texts(file_id) WHERE status = 'failed';

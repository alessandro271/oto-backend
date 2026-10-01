CREATE TABLE IF NOT EXISTS doc_embeddings (
doc_id BIGINT PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
content_sha TEXT NOT NULL,
embedding halfvec(1024) NOT NULL,
model TEXT NOT NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_embeddings_hnsw
ON doc_embeddings USING hnsw (embedding halfvec_cosine_ops);
CREATE TABLE IF NOT EXISTS aux_embeddings (
kind TEXT NOT NULL,
ref BIGINT NOT NULL,
content_sha TEXT NOT NULL,
embedding halfvec(1024) NOT NULL,
model TEXT NOT NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_aux_embeddings_hnsw
ON aux_embeddings USING hnsw (embedding halfvec_cosine_ops);
CREATE TABLE IF NOT EXISTS doc_chunk_embeddings (
doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
chunk_index INT NOT NULL,
content_sha TEXT NOT NULL,
embedding halfvec(1024) NOT NULL,
model TEXT NOT NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_doc_chunk_embeddings_hnsw
ON doc_chunk_embeddings USING hnsw (embedding halfvec_cosine_ops);
CREATE TABLE IF NOT EXISTS datastore_row_embeddings (
ns_id BIGINT NOT NULL,
row_id TEXT NOT NULL,
content_sha TEXT NOT NULL,
embedding halfvec(1024) NOT NULL,
model TEXT NOT NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (ns_id, row_id),
FOREIGN KEY (ns_id, row_id) REFERENCES datastore_rows(ns_id, row_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_datastore_row_embeddings_hnsw
ON datastore_row_embeddings USING hnsw (embedding halfvec_cosine_ops);

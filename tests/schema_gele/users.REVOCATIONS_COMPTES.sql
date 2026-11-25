CREATE TABLE IF NOT EXISTS revocations_comptes (
sub TEXT PRIMARY KEY,
revoque_avant TIMESTAMPTZ NOT NULL,
motif TEXT NOT NULL
);

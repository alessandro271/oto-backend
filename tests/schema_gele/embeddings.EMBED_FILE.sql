CREATE TABLE IF NOT EXISTS embed_sources (
source TEXT NOT NULL,
ref TEXT NOT NULL,
bail_jusqu_a TIMESTAMPTZ,
sha TEXT,
tentatives INTEGER NOT NULL DEFAULT 0,
echec_raison TEXT,
echec_at TIMESTAMPTZ,
PRIMARY KEY (source, ref)
);
CREATE TABLE IF NOT EXISTS embed_consommation (
jour DATE PRIMARY KEY,
tokens BIGINT NOT NULL DEFAULT 0,
plafond_atteint_at TIMESTAMPTZ
);

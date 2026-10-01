CREATE TABLE IF NOT EXISTS portee_elargissements (
id BIGSERIAL PRIMARY KEY,
acteur_sub TEXT NOT NULL,
org_id BIGINT,
ressource_type TEXT NOT NULL,
ressource_id TEXT NOT NULL,
ressource_nom TEXT,
proprietaire_sub TEXT,
vers TEXT NOT NULL,
cible TEXT,
geste TEXT NOT NULL,
destinataires JSONB NOT NULL DEFAULT '[]'::jsonb,
immediat BOOLEAN NOT NULL DEFAULT false,
notifie_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_portee_created ON portee_elargissements (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_portee_proprietaire
ON portee_elargissements (proprietaire_sub, created_at DESC);

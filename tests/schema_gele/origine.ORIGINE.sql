CREATE TABLE IF NOT EXISTS origine_ecritures (
sub TEXT,
org_id BIGINT,
ns_id BIGINT NOT NULL,
colonne TEXT NOT NULL,
face TEXT,
format_declare BOOLEAN NOT NULL DEFAULT false,
ecritures BIGINT NOT NULL DEFAULT 1,
ecritures_declarees BIGINT NOT NULL DEFAULT 0,
derniere_declaree_at TIMESTAMPTZ,
premiere_at TIMESTAMPTZ NOT NULL DEFAULT now(),
derniere_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_origine_ecritures_qui
ON origine_ecritures (COALESCE(sub, ''), ns_id, colonne);
CREATE INDEX IF NOT EXISTS idx_origine_ecritures_fraicheur
ON origine_ecritures (derniere_at DESC);

CREATE TABLE IF NOT EXISTS limite_refus_jour (
jour DATE NOT NULL,
env TEXT NOT NULL,
limiteur TEXT NOT NULL,
classe TEXT NOT NULL,
cle TEXT NOT NULL,
refus BIGINT NOT NULL,
mode TEXT NOT NULL,
premier_at TIMESTAMPTZ NOT NULL,
dernier_at TIMESTAMPTZ NOT NULL,
PRIMARY KEY (jour, env, limiteur, classe, cle)
);

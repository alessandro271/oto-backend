"""DDL du domaine « embeddings » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# vecteurs (pages, sources aux, chunks, lignes)
EMBEDDINGS = """
-- Embeddings des pages (lot 3, recherche sémantique V2) — une ligne par doc,
-- mistral-embed 1024 en halfvec. `content_sha` = idempotence (ré-embed seulement si
-- le texte change). Table NEUVE → l'index HNSW ici est sûr (créée juste au-dessus).
CREATE TABLE IF NOT EXISTS doc_embeddings (
    doc_id BIGINT PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    content_sha TEXT NOT NULL,
    embedding halfvec(1024) NOT NULL,
    model TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_embeddings_hnsw
    ON doc_embeddings USING hnsw (embedding halfvec_cosine_ops);

-- Embeddings des sources NON-page (oto/#6 C) : briefs de projet + guides on-demand.
-- Table générique keyée (kind, ref) — ref = projects.id (brief) | nodes.id (node) ;
-- même modèle 1024d que doc_embeddings. Le worker draine `embed_dirty` de projects/nodes.
CREATE TABLE IF NOT EXISTS aux_embeddings (
    kind TEXT NOT NULL,                          -- 'brief' | 'node'
    ref BIGINT NOT NULL,                         -- projects.id | nodes.id
    content_sha TEXT NOT NULL,
    embedding halfvec(1024) NOT NULL,
    model TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_aux_embeddings_hnsw
    ON aux_embeddings USING hnsw (embedding halfvec_cosine_ops);

-- Chunks de DÉBORDEMENT d'une page longue (oto/#6 C) : au-delà du 1er morceau (16k,
-- dans doc_embeddings), les chunks 1..N vivent ici → toute la page est recherchable.
-- Additif : doc_embeddings inchangé (rétro-compat). CASCADE sur la page.
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

-- Embeddings des LIGNES de datastore (#67 V2.2) — sémantique OPT-IN par namespace
-- (flag `user_datastores.semantic_search`). Une ligne = un vecteur (JSON rendu), même
-- modèle 1024d. Le worker draine `datastore_rows.embed_dirty` des namespaces opt-in.
-- Table NEUVE → HNSW sûr ici. CASCADE sur la row (FK composite sur sa PK).
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
"""

# La file d'indexation : réservations, échecs, consommation du jour — fragment séparé :
# la révision Alembic `20261123_1433_embed_file` l'exécute tel quel (une seule écriture
# du DDL), et le démarrage d'une base neuve le pose avec le reste.
EMBED_FILE = """
-- L'état d'une source dans la file d'indexation (`embed_worker`), keyée comme le worker
-- la nomme : `source` = 'doc' | 'brief' | 'node' | 'row', `ref` = son identifiant en
-- texte ('<ns_id>/<row_id>' pour une ligne). Une ligne n'existe que pour une source
-- réservée ou en échec ; un succès l'efface.
-- `bail_jusqu_a` : réservation d'un processus — une source sous bail n'est prise par
-- aucun autre. `tentatives` : échecs passagers d'affilée sur le texte `sha` ; au-delà
-- de la borne du worker, la source sort de la file. `echec_raison` non NULL : la
-- source est sortie de la file en échec (son drapeau d'indexation baissé), avec la
-- réponse du fournisseur ; une nouvelle écriture de la source la remet dans la file.
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

-- Les tokens d'embedding facturés par jour (UTC) à CETTE instance, tels que le
-- fournisseur les compte (`usage.prompt_tokens`). Le worker s'arrête pour la journée
-- au plafond (`OTO_EMBED_PLAFOND_TOKENS_JOUR`) ; `plafond_atteint_at` le dit, et sa
-- pose (une seule fois par jour) déclenche l'alerte d'équipe.
CREATE TABLE IF NOT EXISTS embed_consommation (
    jour DATE PRIMARY KEY,
    tokens BIGINT NOT NULL DEFAULT 0,
    plafond_atteint_at TIMESTAMPTZ
);
"""

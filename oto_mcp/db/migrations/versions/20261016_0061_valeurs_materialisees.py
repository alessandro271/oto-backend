"""Le texte des valeurs servies, matérialisé par ligne : la recherche `values` suit `limit`.

La recherche `q_scope=values` revérifiait chaque candidat de l'index trigramme en
recalculant `datastore_valeurs_texte_v1(data)`, puis la recalculait encore pour le
classement et le total : son coût suivait le nombre de lignes TROUVÉES (19-22 s en prod
pour ~1 200 lignes d'un tableau large, `limit=8`). Cette révision pose, sur
`datastore_rows` (`db/valeurs_servies.py`) :

- deux colonnes nullables sans défaut : `valeurs_texte text` (le texte replié) et
  `valeurs_vec tsvector` (son vecteur) — catalogue seul, aucune réécriture ;
- la fonction `datastore_valeurs_materialisees()` et le déclencheur
  `datastore_rows_25_valeurs` (`BEFORE INSERT OR UPDATE OF data`), qui les remplit à
  chaque écriture de `data`, quel que soit le code qui écrit ;
- deux GIN, CONCURRENTLY : `idx_datastore_rows_valeurs_vec` (`valeurs_vec`) et
  `idx_datastore_rows_valeurs_texte_trgm` (`valeurs_texte gin_trgm_ops`).

Les lignes nées avant restent vides : `scripts/remplir_valeurs_servies.py` les remplit
(à blanc par défaut). Vides, elles restent TROUVÉES, par l'ancien prédicat.

**Verrous.**
- colonnes (×2) : `AccessExclusiveLock` bref (catalogue seul), seulement si elles
  manquent, attente bornée à 5 s ;
- déclencheur : `SHARE ROW EXCLUSIVE` bref (bloque les écritures le temps de l'ordre),
  seulement s'il manque, attente bornée à 5 s ;
- fonction : aucun verrou de table ;
- index (×2) : `ShareUpdateExclusiveLock`, ni lectures ni écritures bloquées, attente
  bornée à 5 min (`index_concurrent.poser_par_revision`). `datastore_rows` au-delà de
  10 000 lignes → `ConstructionManuelleRequise` : le geste est
  `oto-mcp maintenance index-concurrents 0061`, puis `oto-mcp migrer upgrade head`. Les
  colonnes et le déclencheur, joués avant, sont acquis (la révision se rejoue).

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes colonnes, fonction et
déclencheur (`valeurs_servies.poser`, même verdict), et les index sur une base neuve. Le
code ne LIT les colonnes qu'une fois leurs deux index valides (`valeurs_servies.pretes`) :
avant, l'ancien chemin, à l'identique. Prod et préprod partagent la base : jouée une
fois, elle sert les deux.

**Retour arrière** : retire les index (CONCURRENTLY), le déclencheur, la fonction et les
colonnes. Le code qui les lit retombe sur l'ancien chemin dès que les index manquent.

Révision : 0061_valeurs_materialisees
Précédente : 0060_services_pretes
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, valeurs_servies

revision = valeurs_servies.REVISION
down_revision = "0060_services_pretes"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def _scalaire(sql: str):
    return op.get_bind().exec_driver_sql(sql).scalar()


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ordre in valeurs_servies.ordres_a_jouer(_scalaire):
        op.execute(ordre)
    for index in valeurs_servies.INDEX:
        index_concurrent.poser_par_revision(op, index)


def downgrade() -> None:
    for index in valeurs_servies.INDEX:
        index_concurrent.retirer_par_revision(op, index)
    op.execute(_ATTENTE_MAX)
    for ordre in valeurs_servies.DDL_RETRAIT:
        op.execute(ordre)

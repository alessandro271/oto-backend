"""datastore_rows.claimed_at + idx_datastore_rows_file : l'ordre de service de la file (oto#101).

La file de travail servait `ORDER BY row_id` — la plus ancienne ligne éligible —, sans
mémoire de ce qu'elle venait de servir : une ligne relâchée revenait en tête, et des
workers tournaient indéfiniment sur deux ou trois lignes. Elle sert désormais la ligne
servie le MOINS récemment : `claimed_at ASC NULLS FIRST, row_id`
(`db/rowlock.py::_ORDRE_DE_SERVICE`).

1. `claimed_at TIMESTAMPTZ` NULLABLE, sans défaut : écriture de catalogue seule, aucune
   réécriture. Toutes les lignes existantes valent NULL — « jamais servie » —, ce qui
   est l'ordre d'avant entre elles (départage par `row_id`) ; chacune prend sa date à
   sa prochaine prise. L'`ALTER` prend un `AccessExclusiveLock` sur `datastore_rows`,
   que chaque écriture de ligne touche : d'où le `lock_timeout`, un échec net qu'on
   rejoue.
2. `idx_datastore_rows_file (ns_id, claimed_at ASC NULLS FIRST, row_id) WHERE
   abandon_reason IS NULL` — la clé exacte du pick, pour qu'une réservation ne trie
   pas tout le tableau. **CONCURRENTLY** : un `CREATE INDEX` ordinaire bloquerait les
   écritures de `datastore_rows` le temps du parcours ; refusé dans un bloc
   transactionnel, d'où `autocommit_block()` (même régime que 0002). Un échec en cours
   de construction laisse un index INVALIDE : le retirer (`DROP INDEX CONCURRENTLY`)
   avant de rejouer, `IF NOT EXISTS` le prendrait pour posé.

Le démarrage pose la même colonne et le même index s'il ne les trouve pas
(`rowlock.DDL_COLONNE_DERNIERE_PRISE`, `rowlock.DDL_INDEX_FILE`, sous garde de
catalogue) : c'est le chemin d'une base NEUVE, estampillée à la tête. Sur une base
peuplée, l'index de démarrage n'est PAS concurrent : jouer cette révision **avant la
fusion**, pour que le démarrage n'ait plus rien à construire.

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit ni n'écrit `claimed_at`
et ne connaît pas l'index (son pick par `row_id` passe par la clé primaire) : jouer la
révision avant le déploiement est sûr. Le nouveau code ÉCRIT la colonne à chaque
réservation. Le retour arrière retire l'index puis la colonne ; le code qui les écrit
doit être retiré AVANT, sinon chaque réservation échoue.

Révision : 0030_file_de_travail_ordre
Précédente : 0029_cle_metier_valeur_servie
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.rowlock import (DDL_COLONNE_DERNIERE_PRISE, DDL_INDEX_FILE_CONCURRENT,
                                INDEX_FILE)

revision = "0030_file_de_travail_ordre"
down_revision = "0029_cle_metier_valeur_servie"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_DERNIERE_PRISE)
    with op.get_context().autocommit_block():
        op.execute(DDL_INDEX_FILE_CONCURRENT)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_FILE}")
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE datastore_rows DROP COLUMN IF EXISTS claimed_at")

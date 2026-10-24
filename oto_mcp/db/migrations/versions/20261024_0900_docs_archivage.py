"""L'archivage et la date de vérification d'une page (docs/projects.md §Archiver une page).

Trois colonnes NEUVES, nullables, sans défaut — le DDL est
`db/doc_archivage.py::DDL_COLONNES`, exécuté tel quel : `docs.archived_at TIMESTAMPTZ`,
`docs.verified_at TIMESTAMPTZ`, `docs.verified_by TEXT`, en un seul `ALTER TABLE`. NULL =
page vivante, jamais vérifiée : l'état de toutes les pages d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `docs` (colonnes nullables sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : `docs` est lue à chaque
lecture de page et par la recherche ; un échec net, qu'on rejoue, plutôt qu'une attente
qui bloquerait ces lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit les colonnes (`_DOC_COLS`,
la recherche), mais le démarrage les pose avant de servir (`ADD COLUMN IF NOT EXISTS`,
même DDL, sous garde de catalogue) ; l'ancien code les ignore — il sert donc encore une
page archivée dans sa recherche et ses listes, le temps de la bascule.

Retour arrière : retire les colonnes (les archivages et les dates de vérification posés
se perdent : toute page redevient vivante).

Révision : 20261024_0900_docs_archivage
Précédente : 20261023_2000_liens_agents
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.doc_archivage import COLONNES, DDL_COLONNES

revision = "20261024_0900_docs_archivage"
down_revision = "20261023_2000_liens_agents"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE docs " + ", ".join(
        f"DROP COLUMN IF EXISTS {c}" for c in COLONNES))

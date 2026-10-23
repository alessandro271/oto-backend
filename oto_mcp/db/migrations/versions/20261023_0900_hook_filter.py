"""Le filtre de livraison d'un webhook (docs/runner-et-automatisations.md §Le FILTRE de livraison).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_triggers.py::DDL_COLONNE_HOOK_FILTER`, exécuté tel quel :
`runner_triggers.hook_filter JSONB`. `{chemin: [valeurs]}` qu'une livraison
AUTHENTIFIÉE doit porter pour lancer un run ; NULL = tout passe, l'état de tous les
agents d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table
en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne (`_COLS`),
mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous
garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne. Les filtres posés sont perdus : chaque agent
redevient « tout passe ».

Révision : 20261023_0900_hook_filter
Précédente : 20261021_1615_links_position
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_HOOK_FILTER, DDL_COLONNE_HOOK_FILTER

revision = "20261023_0900_hook_filter"
down_revision = "20261021_1615_links_position"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_HOOK_FILTER)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_HOOK_FILTER}")

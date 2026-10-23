"""La date du dernier changement d'un agent (docs/runner-et-automatisations.md §Dater un
changement).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_triggers.py::DDL_COLONNE_UPDATED_AT`, exécuté tel quel :
`runner_triggers.updated_at TIMESTAMPTZ`. Posée par chaque écriture de configuration
(allumer, éteindre, horaire, réglage, adresse, propriétaire), jamais par le tick ; NULL =
aucun changement depuis la création, l'état de tous les agents d'avant. Sans défaut
exprès : `DEFAULT NOW()` daterait chaque agent existant du jour de la migration, une
date fausse donnée pour vraie.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table
en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit et écrit la colonne
(`_COLS`, les écritures de configuration), mais le démarrage la pose avant de servir
(`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code
l'ignore.

Retour arrière : retire la colonne (les dates posées se perdent, les agents reviennent
« sans changement daté »).

Révision : 20261023_1033_trigger_updated_at
Précédente : 20261023_0713_lemlist_personas
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_UPDATED_AT, DDL_COLONNE_UPDATED_AT

revision = "20261023_1033_trigger_updated_at"
down_revision = "20261023_0713_lemlist_personas"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_UPDATED_AT)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_UPDATED_AT}")

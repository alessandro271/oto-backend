"""L'épisode de blocage annoncé d'un agent (docs/runner-et-automatisations.md §Prévenir
quand un agent est bloqué).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/alertes_agents.py::DDL_COLONNE_ECHEC_ANNONCE`, exécuté tel quel :
`runner_triggers.echec_annonce_at TIMESTAMPTZ`. La date de l'annonce du dernier épisode
de travaux morts consécutifs ; NULL = aucun annoncé, l'état de tous les agents d'avant —
dont l'épisode en cours, s'il y en a un, sera donc annoncé une fois.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table
en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit et écrit la colonne
(`db/alertes_agents.py`), mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT
EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les annonces posées se perdent : l'épisode en cours
de chaque agent bloqué serait annoncé une seconde fois).

Révision : 20261023_1040_echec_annonce
Précédente : 20261023_1033_trigger_updated_at
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.alertes_agents import COLONNE_ECHEC_ANNONCE, DDL_COLONNE_ECHEC_ANNONCE

revision = "20261023_1040_echec_annonce"
down_revision = "20261023_1033_trigger_updated_at"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_ECHEC_ANNONCE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_ECHEC_ANNONCE}")

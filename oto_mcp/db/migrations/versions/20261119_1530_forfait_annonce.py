"""L'attente d'abonnement annoncée d'un agent (docs/runner-et-automatisations.md §Prévenir
quand un agent est bloqué).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/alertes_agents.py::DDL_COLONNE_FORFAIT_ANNONCE`, exécuté tel quel :
`runner_triggers.forfait_annonce_at TIMESTAMPTZ`. La date de l'annonce du dernier
épisode où les travaux d'un agent ATTENDENT un abonnement personnel à reconnecter ;
NULL = aucune annoncée, l'état de tous les agents d'avant. Elle est distincte de
`echec_annonce_at` (l'épisode de travaux morts) : chacun des deux épisodes se rouvre sur
sa propre preuve, et une marque commune ferait taire l'un quand l'autre a été annoncé.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table
en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit et écrit la colonne
(`db/alertes_agents.py`), mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT
EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les annonces posées se perdent : l'attente en cours
de chaque agent serait annoncée une seconde fois).

Révision : 20261119_1530_forfait_annonce
Précédente : 20261119_1400_sending_limits
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.alertes_agents import COLONNE_FORFAIT_ANNONCE, DDL_COLONNE_FORFAIT_ANNONCE

revision = "20261119_1530_forfait_annonce"
down_revision = "20261119_1400_sending_limits"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_FORFAIT_ANNONCE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_FORFAIT_ANNONCE}")

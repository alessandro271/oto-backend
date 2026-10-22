"""La fenêtre dont une pause de forfait attend l'échéance (docs/runner-et-automatisations.md,
§ La sonde avant le run).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/user_subscriptions.py::DDL_COLONNE_FENETRE`, exécuté tel quel :
`user_model_subscriptions.limit_fenetre TEXT`. La fenêtre que le fournisseur nomme sur un
refus, ou celle qui a passé le seuil ; NULL = inconnue, l'état de toutes les pauses d'avant
— qui restent donc revérifiables par la sonde.

**Verrous.** `AccessExclusiveLock` bref sur `user_model_subscriptions` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : la réservation lit cette
table à chaque prise ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait les
réservations derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit et écrit la colonne
(`get_subscription`, `marquer_statut`, la réservation), mais le démarrage la pose avant de
servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code
l'ignore.

Retour arrière : retire la colonne (les fenêtres posées se perdent, toute pause redevient
de cause inconnue, donc revérifiable).

Révision : 20261022_2112_forfait_fenetre
Précédente : 20261022_2100_recipes_display
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.user_subscriptions import COLONNE_FENETRE, DDL_COLONNE_FENETRE

revision = "20261022_2112_forfait_fenetre"
down_revision = "20261022_2100_recipes_display"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_FENETRE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE user_model_subscriptions DROP COLUMN IF EXISTS {COLONNE_FENETRE}")

"""Le partage par défaut d'une org (docs/runner-et-automatisations.md §Partage).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_triggers.py::DDL_COLONNE_PARTAGE_PAR_DEFAUT`, exécuté tel quel :
`orgs.default_sharing JSONB`. NULL = un objet neuf de l'org naît privé, l'état de toutes
les orgs d'avant. Seul un admin de l'org la pose (`oto_org_settings
domain=partage_par_defaut`).

**Verrous.** `AccessExclusiveLock` bref sur `orgs` (colonne nullable sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : `orgs` est lue à chaque requête ;
un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ces lectures.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne, mais le démarrage
la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ;
l'ancien code l'ignore.

Retour arrière : retire la colonne (les réglages posés se perdent, tout agent neuf naît
privé ; les partages déjà nés du réglage restent, marqués).

Révision : 20261024_1101_default_sharing
Précédente : 20261024_1100_pool_sans_mode
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import (COLONNE_PARTAGE_PAR_DEFAUT,
                                        DDL_COLONNE_PARTAGE_PAR_DEFAUT)

revision = "20261024_1101_default_sharing"
down_revision = "20261024_1100_pool_sans_mode"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_PARTAGE_PAR_DEFAUT)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE_PARTAGE_PAR_DEFAUT}")

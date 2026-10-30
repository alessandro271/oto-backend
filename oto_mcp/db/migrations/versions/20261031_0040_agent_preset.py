"""Le préréglage d'une personne dans une org (docs/runner-et-automatisations.md §Préréglage).

Une colonne NEUVE sur l'appartenance — le DDL est
`db/runner_triggers.py::DDL_COLONNE_PRESET_PARTAGE`, exécuté tel quel :
`org_members.agent_preset_shared BOOLEAN NOT NULL DEFAULT FALSE`. FALSE = ses agents
naissent privés, l'état de toutes les appartenances d'avant. Seule la personne la pose
(`oto_trigger op=default_sharing`).

**Verrous.** `AccessExclusiveLock` bref sur `org_members` (défaut constant : catalogue
seul, aucune réécriture). `lock_timeout` 5 s : `org_members` est lue à chaque requête ; un
échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ces lectures.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne, mais le démarrage
la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ;
l'ancien code l'ignore.

Retour arrière : retire la colonne (les préréglages posés se perdent, tout agent neuf naît
privé ; les partages et prêts déjà nés restent, posés par leur propriétaire).

Révision : 20261031_0040_agent_preset
Précédente : 20261030_1500_slack_agent_posts
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_PRESET_PARTAGE, DDL_COLONNE_PRESET_PARTAGE

revision = "20261031_0040_agent_preset"
down_revision = "20261030_1500_slack_agent_posts"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_PRESET_PARTAGE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE org_members DROP COLUMN IF EXISTS {COLONNE_PRESET_PARTAGE}")

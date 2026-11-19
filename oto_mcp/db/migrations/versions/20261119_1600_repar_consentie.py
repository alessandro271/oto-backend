"""La réparation automatique consentie par l'org, et les outils qu'une personne a
retirés après le médecin des agents (docs/runner-et-automatisations.md §Médecin des
agents).

Deux objets NEUFS, posés aussi par le démarrage (même DDL, ordre indifférent) :

- la table `agent_health_settings` (`db/schema/runs.py::AGENT_HEALTH_SETTINGS`) : une
  ligne par org qui a choisi, `auto_repair BOOLEAN NOT NULL DEFAULT FALSE`. Aucune ligne
  = réparation automatique COUPÉE : un admin de l'org l'allume. Aucune FK, aucune
  colonne de compte ;
- la colonne `runner_triggers.repair_undone_tools JSONB` (`db/runner_triggers.py::
  DDL_COLONNE_OUTILS_ANNULES`), nullable, sans défaut : `{outil: jusqu_a}`, les outils
  que le médecin ne redonne pas à l'agent jusqu'à cette date.

**Verrous.** La table est neuve et vide. La colonne prend un `AccessExclusiveLock` bref
sur `runner_triggers` (nullable sans défaut : catalogue seul, aucune réécriture).
`lock_timeout` 5 s : le tick lit cette table en continu ; un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes objets avant de
servir ; l'ancien code les ignore.

Retour arrière : retire la colonne puis la table (les choix des orgs se perdent : toutes
reviennent au défaut, coupé ; les retraits d'outils se perdent : le médecin pourrait de
nouveau les donner — les épisodes, qui journalisent l'annulation, restent).

Révision : 20261119_1600_repar_consentie
Précédente : 20261119_1530_forfait_annonce
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_OUTILS_ANNULES, DDL_COLONNE_OUTILS_ANNULES
from oto_mcp.db.schema.runs import AGENT_HEALTH_SETTINGS

revision = "20261119_1600_repar_consentie"
down_revision = "20261119_1530_forfait_annonce"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(AGENT_HEALTH_SETTINGS)
    op.execute(DDL_COLONNE_OUTILS_ANNULES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_OUTILS_ANNULES}")
    op.execute("DROP TABLE IF EXISTS agent_health_settings")

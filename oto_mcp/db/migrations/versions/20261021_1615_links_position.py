"""Le rang d'affichage d'un lien de projet (docs/projects.md §Rang d'affichage d'un lien).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/projects.py::DDL_COLONNE_POSITION_LIEN`, exécuté tel quel : `project_links.position
DOUBLE PRECISION`. Le rang du lien parmi ceux du même type dans les listes du projet ;
NULL = non rangé, l'état de tous les liens d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `project_links` (colonne nullable sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : les liens sont lus à chaque
lecture de projet ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ces
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne
(`list_project_links_many`), mais le démarrage la pose avant de servir (`ADD COLUMN IF
NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les rangs posés se perdent, les liens reviennent à
l'ordre d'avant).

Révision : 20261021_1615_links_position
Précédente : 20261021_1352_outils_act_envois
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.projects import COLONNE_POSITION_LIEN, DDL_COLONNE_POSITION_LIEN

revision = "20261021_1615_links_position"
down_revision = "20261021_1352_outils_act_envois"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_POSITION_LIEN)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE project_links DROP COLUMN IF EXISTS {COLONNE_POSITION_LIEN}")

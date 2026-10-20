"""Le rang d'affichage d'un projet (docs/projects.md §Rang d'affichage).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/projects.py::DDL_COLONNE_POSITION`, exécuté tel quel : `projects.position DOUBLE
PRECISION`. Le rang du projet parmi ceux de son dossier dans les listes ; NULL = non
rangé, l'état de tous les projets d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `projects` (colonne nullable sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : `projects` est lue à chaque
requête qui touche un projet ; un échec net, qu'on rejoue, plutôt qu'une attente qui
bloquerait ces lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne
(`_PROJECT_COLS`), mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT
EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les rangs posés se perdent, les listes reviennent à
l'ordre des noms).

Révision : 0069_projects_position
Précédente : 0068_credits
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.projects import COLONNE_POSITION, DDL_COLONNE_POSITION

revision = "0069_projects_position"
down_revision = "0068_credits"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_POSITION)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE projects DROP COLUMN IF EXISTS {COLONNE_POSITION}")

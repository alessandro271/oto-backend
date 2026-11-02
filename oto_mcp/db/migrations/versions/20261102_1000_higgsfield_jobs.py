"""Les travaux Higgsfield de la clé plateforme, notés à la soumission (`db/higgsfield_jobs.py`).

Une table NEUVE, née entière, et deux index — le DDL est le fragment
`db/schema/usage.py::HIGGSFIELD_JOBS`, exécuté tel quel.

**Verrous.** `AccessExclusiveLock` sur la table neuve seulement (vide, instantané),
aucune clé étrangère. `lock_timeout` 5 s par habitude de la maison.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE IF
NOT EXISTS`, même fragment) ; l'ancien code l'ignore. La soumission d'un travail n'en
dépend pas : une écriture ratée est journalisée, le travail part quand même.

Retour arrière : retire la table (les travaux non relus redeviennent invisibles, le prix
de ceux déjà facturés reste dans le journal).

Révision : 20261102_1000_higgsfield_jobs
Précédente : 20261031_1000_apify_runs
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import HIGGSFIELD_JOBS

revision = "20261102_1000_higgsfield_jobs"
down_revision = "20261031_1000_apify_runs"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(HIGGSFIELD_JOBS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS higgsfield_jobs")

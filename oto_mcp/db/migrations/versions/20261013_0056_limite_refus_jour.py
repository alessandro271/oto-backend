"""Palmarès quotidien des refus des limiteurs (#1232, lot 1).

Une table NEUVE, née entière — le DDL est le fragment
`db/schema/usage.py::LIMITE_REFUS_JOUR`, exécuté tel quel :
`limite_refus_jour (jour, env, limiteur, classe, cle) PK, refus, mode, premier_at,
dernier_at`.

**Verrous.** `AccessExclusiveLock` sur la table neuve seule (vide, instantané).

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment), y compris sur une base existante ; l'écriture
(`limite_refus_top.py`) part sur un fil à part et journalise son échec sans rien
casser.

Retour arrière : retire la table (une mesure de trente jours au plus).

Révision : 0056_limite_refus_jour
Précédente : 0055_platform_key_rate
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import LIMITE_REFUS_JOUR

revision = "0056_limite_refus_jour"
down_revision = "0055_platform_key_rate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(LIMITE_REFUS_JOUR)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS limite_refus_jour")

"""Fenêtre de débit d'une org sur une clé plateforme partagée (F-051).

Une table NEUVE, née entière — le DDL est le fragment
`db/schema/usage.py::PLATFORM_KEY_RATE`, exécuté tel quel :
`platform_key_rate (scope, connector) PK, window_start, calls`.

**Verrous.** `AccessExclusiveLock` sur la table neuve seule (vide, instantané).

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment), y compris sur une base existante ; le compteur qui
l'écrit (`access/platform_rate.py`) est fail-open : sans la table, il journalise et
sert l'appel.

Retour arrière : retire la table (un compteur d'une minute, rien à garder).

Révision : 0055_platform_key_rate
Précédente : 0054_tool_calls_gestes_de_ligne
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import PLATFORM_KEY_RATE

revision = "0055_platform_key_rate"
down_revision = "0054_tool_calls_gestes_de_ligne"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(PLATFORM_KEY_RATE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_key_rate")

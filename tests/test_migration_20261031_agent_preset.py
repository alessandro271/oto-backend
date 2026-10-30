"""La révision `20261031_0040_agent_preset` pose `org_members.agent_preset_shared`.

Une colonne NEUVE que le démarrage pose aussi (même DDL) : la révision doit la poser sur
une base d'AVANT, ne rien faire sur une base que le démarrage a déjà servie, et la retirer
au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261030_1500_slack_agent_posts"
_REVISION = "20261031_0040_agent_preset"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _colonne(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'org_members' AND column_name = 'agent_preset_shared'"
        ).fetchall()


_POSEE = [("boolean", "NO", "false")]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE org_members DROP COLUMN agent_preset_shared")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_la_colonne_et_le_retour_arriere_la_retire(base_d_avant):
    from alembic import command

    assert _colonne(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(base_d_avant) == _POSEE
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _colonne(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(base_d_avant) == _POSEE


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # la colonne est là : IF NOT EXISTS
    assert _colonne(base_d_avant) == _POSEE

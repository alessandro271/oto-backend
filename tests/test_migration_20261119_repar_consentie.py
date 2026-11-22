"""La révision `20261119_1600_repar_consentie` pose la table `agent_health_settings` et
la colonne `runner_triggers.repair_undone_tools`.

Deux objets NEUFS que le démarrage pose aussi (même DDL) : la révision doit les poser
sur une base d'AVANT (qui ne les a pas), ne rien faire sur une base que le démarrage a
déjà servie, et les retirer au retour arrière. La réparation automatique y naît COUPÉE
(`auto_repair` DEFAULT FALSE). Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261119_1530_forfait_annonce"
_REVISION = "20261119_1600_repar_consentie"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _etat(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        colonne = c.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'runner_triggers' AND column_name = 'repair_undone_tools'"
        ).fetchall()
        table = c.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'agent_health_settings' "
            "ORDER BY ordinal_position").fetchall()
    return colonne, table


_POSES = ([("jsonb", "YES", None)],
          [("org_id", "bigint", "NO", None), ("auto_repair", "boolean", "NO", "false"),
           ("updated_at", "timestamp with time zone", "NO", "now()")])


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : objets retirés, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE runner_triggers DROP COLUMN repair_undone_tools")
        c.execute("DROP TABLE agent_health_settings")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_les_objets_et_le_retour_arriere_les_retire(base_d_avant):
    from alembic import command

    assert _etat(base_d_avant) == ([], [])
    command.upgrade(_alembic(), _REVISION)
    assert _etat(base_d_avant) == _POSES
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _etat(base_d_avant) == ([], [])
    command.upgrade(_alembic(), _REVISION)
    assert _etat(base_d_avant) == _POSES


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser les objets avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # IF NOT EXISTS
    assert _etat(base_d_avant) == _POSES


def test_une_org_sans_ligne_n_a_pas_la_reparation(base_d_avant):
    import psycopg
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    with psycopg.connect(base_d_avant, autocommit=True) as c:
        c.execute("INSERT INTO agent_health_settings (org_id) VALUES (987654) "
                  "ON CONFLICT DO NOTHING")
        (v,) = c.execute("SELECT auto_repair FROM agent_health_settings "
                         "WHERE org_id = 987654").fetchone()
    assert v is False, "le défaut est coupé"

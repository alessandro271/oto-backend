"""La révision `20261102_1000_higgsfield_jobs` pose la table, et le stockage tient sa promesse.

Joué sur une vraie base : la révision pose la table sur une base d'AVANT, le premier état
d'un travail reste, et la ligne du relevé est celle que le facturant lit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261031_1000_apify_runs"
_REVISION = "20261102_1000_higgsfield_jobs"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _existe(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass('higgsfield_jobs') AS t").fetchone()[0] is not None


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE higgsfield_jobs")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_et_le_retour_arriere_retire(base_d_avant):
    from alembic import command
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)
    assert _existe(base_d_avant)
    command.downgrade(_alembic(), _PRECEDENTE)
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)


def test_le_cycle_d_un_travail(base_d_avant):
    from oto_mcp.db import higgsfield_jobs as t
    t.noter("j1", org_id=7, sub="s", run_id=None, model="m/a", prix_microdollars=560000,
            approximatif=False)
    t.noter("j1", org_id=8, sub="x", run_id=None, model="m/b", prix_microdollars=1,
            approximatif=True)
    ligne = t.lire("j1")
    assert ligne["org_id"] == 7 and ligne["prix_microdollars"] == 560000
    assert ligne["approximatif"] is False
    assert t.lire("absent") is None
    assert [r["request_id"] for r in t.a_relever()] == ["j1"]

    t.marquer("j1", statut="completed")
    t.marquer("j1", statut="failed")
    assert t.lire("j1")["statut"] == "completed"
    assert t.a_relever() == []
    assert t.purger() == 0
    assert t.purger(jours=0) == 1


def test_un_travail_releve_se_lit_dans_la_lentille_de_l_org_qui_l_a_soumis(base_d_avant):
    """Le point de tout : la ligne du relevé est celle que le facturant lit, sous l'org
    qui a soumis, avec le travail pour job — et rien sous une autre org."""
    from oto_mcp import higgsfield_releve
    from oto_mcp.db import usage
    higgsfield_releve._tracer({"request_id": "j2", "sub": "s", "org_id": 7311,
                               "run_id": None, "prix_microdollars": 560000})

    appels = usage.list_billable_calls_for_org(7311, "higgsfield")["calls"]
    assert [(a["job_id"], a["quantity"], a["key_mode"]) for a in appels] == [
        ("j2", 560000, "platform")]
    assert usage.list_billable_calls_for_org(7312, "higgsfield")["calls"] == []

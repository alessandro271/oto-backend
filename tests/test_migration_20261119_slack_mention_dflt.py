"""La révision `20261119_1200_slack_mention_dflt` met « mention seule » par défaut.

Sur une base d'AVANT (colonne à défaut FALSE, une org à FALSE) : le défaut passe à TRUE
et l'org existante aussi ; une org neuve naît à TRUE ; rejouée, elle ne fait rien ; le
retour arrière rend le défaut FALSE et remet les orgs à FALSE. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261118_1500_unipile_slots"
_REVISION = "20261119_1200_slack_mention_dflt"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def test_la_revision_est_au_registre_et_suit_la_precedente():
    from alembic.script import ScriptDirectory
    rev = ScriptDirectory.from_config(_alembic()).get_revision(_REVISION)
    assert rev is not None and rev.down_revision == _PRECEDENTE
    assert len(_REVISION) <= 32 and Path(rev.path).stem == _REVISION


def _defaut(c):
    return c.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'orgs' AND column_name = 'slack_mention_only'").fetchone()[0]


def _valeur(c, oid):
    return c.execute("SELECT slack_mention_only FROM orgs WHERE id = %s",
                     (oid,)).fetchone()[0]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : défaut FALSE, une org à FALSE,
    estampillée à la révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE orgs ALTER COLUMN slack_mention_only SET DEFAULT FALSE")
        oid = c.execute("INSERT INTO orgs (name) VALUES ('org d''avant') RETURNING id"
                        ).fetchone()[0]
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn, oid


def test_le_defaut_et_les_orgs_d_avant_passent_a_TRUE_et_le_retour_arriere_les_rend(
        base_d_avant):
    import psycopg
    from alembic import command

    dsn, oid = base_d_avant
    with psycopg.connect(dsn) as c:
        assert (_defaut(c), _valeur(c, oid)) == ("false", False)
    command.upgrade(_alembic(), _REVISION)
    with psycopg.connect(dsn, autocommit=True) as c:
        assert (_defaut(c), _valeur(c, oid)) == ("true", True)
        neuve = c.execute("INSERT INTO orgs (name) VALUES ('neuve') RETURNING id"
                          ).fetchone()[0]
        assert _valeur(c, neuve) is True, "une org neuve naît en « mention seule »"
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # rejouée : rien ne bouge
    command.downgrade(_alembic(), _PRECEDENTE)
    with psycopg.connect(dsn) as c:
        assert (_defaut(c), _valeur(c, oid)) == ("false", False)
    command.upgrade(_alembic(), _REVISION)

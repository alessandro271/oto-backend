"""La révision `0068_credits` pose les tables du budget d'unités sur une base qui ne les a
pas, et son retour arrière les retire. Joué sur une vraie base : une base bootée dont on
retire les tables (le démarrage les pose aussi), estampillée à la révision d'avant.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "0067_runner_jobs_declencheurs"
_REVISION = "0068_credits"
_TABLES = {"credit_scales", "credit_scale_lines", "credit_payer_modes", "credit_balances",
           "credit_holds", "credit_movements"}


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _tables_credits(dsn) -> set[str]:
    import psycopg
    with psycopg.connect(dsn) as c:
        return {r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE tablename LIKE 'credit\\_%'").fetchall()}


@pytest.fixture(scope="module")
def base_sans_credits(live, pg_module_dsn):
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE credit_movements, credit_holds, credit_balances, "
                  "credit_payer_modes, credit_scale_lines, credit_scales")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_puis_retire_les_tables(base_sans_credits):
    from alembic import command

    assert _tables_credits(base_sans_credits) == set()
    command.upgrade(_alembic(), _REVISION)
    assert _tables_credits(base_sans_credits) == _TABLES
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _tables_credits(base_sans_credits) == set()
    command.upgrade(_alembic(), _REVISION)
    assert _tables_credits(base_sans_credits) == _TABLES


def test_le_curseur_porte_la_transaction_qui_a_ecrit(base_sans_credits):
    """`xid` vient de `pg_current_xact_id()` : deux lignes d'une même transaction le
    partagent, une transaction suivante a le sien, plus grand."""
    import psycopg

    with psycopg.connect(base_sans_credits) as c:
        c.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                  "VALUES ('org:1', 'deposit', 10, 'a'), ('org:1', 'deposit', 5, 'b')")
    with psycopg.connect(base_sans_credits) as c:
        c.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                  "VALUES ('org:1', 'deposit', 1, 'c')")
        xids = c.execute("SELECT idempotency_key, xid::text::bigint FROM credit_movements "
                         "ORDER BY id").fetchall()
    assert xids[0][1] == xids[1][1] < xids[2][1]


def test_une_cle_d_idempotence_est_unique_par_payeur(base_sans_credits):
    import psycopg

    with psycopg.connect(base_sans_credits, autocommit=True) as c:
        c.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                  "VALUES ('org:2', 'deposit', 10, 'k')")
        c.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                  "VALUES ('org:3', 'deposit', 10, 'k')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                      "VALUES ('org:2', 'deposit', 7, 'k')")

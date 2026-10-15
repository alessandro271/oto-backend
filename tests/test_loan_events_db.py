"""Le journal des prêts contre un VRAI PostgreSQL : la révision 0058, la pose et le
retrait d'un prêt journalisés dans la transaction du `share_side`, et rien de plus.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
ORG, PRETEUSE, PAIR = 41, "preteuse-x", "pair-y"
LIGNE = ("member", f"{ORG}:{PRETEUSE}", "hunter", "")


@pytest.fixture()
def live(pg_dsn, monkeypatch):
    """Une base JETABLE par test, bootée par le vrai chemin, une ligne de coffre."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_prets_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_pool = dbconn._pool
    monkeypatch.setenv("DATABASE_URL", dsn)
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        _executer(dsn, "INSERT INTO connector_credentials (entity_type, entity_id, "
                       "connector, account, secret_enc) VALUES "
                       f"('{LIGNE[0]}', '{LIGNE[1]}', '{LIGNE[2]}', '', 'chiffre-factice')")
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _executer(dsn: str, *ordres: str):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        return [c.execute(o).fetchall() if o.lstrip().upper().startswith("SELECT")
                else c.execute(o) for o in ordres]


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


_COLONNES = ("SELECT column_name, data_type, is_nullable, column_default "
             "FROM information_schema.columns WHERE table_name = 'connector_loan_events' "
             "ORDER BY ordinal_position")


def test_la_revision_pose_la_table_du_demarrage_et_la_retire(live):
    from alembic import command
    (neuve,) = _executer(live, _COLONNES)
    assert neuve, "le démarrage pose la table sur une base neuve"
    _executer(live, "DROP TABLE connector_loan_events")
    command.stamp(_alembic(), "0057_contraintes_create_table")
    command.upgrade(_alembic(), "0058_journal_des_prets")
    assert _executer(live, _COLONNES)[0] == neuve
    command.downgrade(_alembic(), "0057_contraintes_create_table")
    assert _executer(live, _COLONNES)[0] == []


def test_la_pose_et_le_retrait_d_un_pret_sont_journalises(live, monkeypatch):
    from oto_mcp import access, db
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import sharing
    from oto_mcp.db import loan_events
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(db, "get_user", lambda sub: {"sub": sub})
    ctx = ResolvedCtx(sub=PRETEUSE, org_id=ORG)

    def preter(**k):
        return sharing._lend_instance(ctx, sharing.LendInstanceInput(connector="hunter", **k))

    assert preter(to_org=True)["lent_to_org"] is True
    preter(to_org=True)                       # répété : l'état ne change pas, rien de noté
    preter(to=PAIR)
    sortie = preter(to_org=True, revoke=True)
    assert (sortie["lent_to_org"], sortie["lent_to"]) == (False, [PAIR])
    gestes = loan_events.pour_la_ligne(*LIGNE)
    assert [(g["actor_sub"], g["org_id"], g["target"], g["action"]) for g in gestes] == [
        (PRETEUSE, ORG, f"org:{ORG}", "revoke"),
        (PRETEUSE, ORG, f"user:{PAIR}", "lend"),
        (PRETEUSE, ORG, f"org:{ORG}", "lend")]
    assert all(g["at"] is not None and (g["entity_type"], g["entity_id"], g["connector"],
                                        g["account"]) == LIGNE for g in gestes)
    (colonnes,) = _executer(live, _COLONNES)
    assert not {c[0] for c in colonnes} & {"secret", "secret_enc", "meta"}


def test_un_geste_qui_ne_se_journalise_pas_ne_change_pas_le_partage(live):
    from oto_mcp import credentials_store
    with pytest.raises(ValueError):
        credentials_store.set_instance_sharing(
            *LIGNE, share_side=[f"org:{ORG}"],
            journal=[{"actor_sub": PRETEUSE, "org_id": ORG, "target": f"org:{ORG}",
                      "action": "inconnue"}])
    assert credentials_store.get_instance_sharing(*LIGNE) == ([], [])

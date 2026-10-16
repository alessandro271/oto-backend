"""Les services prêtés contre un VRAI PostgreSQL : la révision 0060 fige les prêts Google
et Microsoft existants sur les services que leur compte a autorisés, et ne touche rien
d'autre ; la pose écrit la liste dans la transaction du `share_side` (signal 1399).

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
ORG, PRETEUSE, PAIR = 41, "preteuse-x", "pair-y"


@pytest.fixture()
def live(pg_dsn, monkeypatch):
    """Une base JETABLE par test, bootée par le vrai chemin."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_services_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_pool = dbconn._pool
    monkeypatch.setenv("DATABASE_URL", dsn)
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _executer(dsn: str, ordre: str, params=None):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        cur = c.execute(ordre, params)
        return cur.fetchall() if ordre.lstrip().upper().startswith("SELECT") else None


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _ligne(dsn, connector, account, meta, side):
    _executer(dsn, "INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                   "account, secret_enc, meta, share_side) VALUES "
                   "('member', %s, %s, %s, 'chiffre-factice', %s::jsonb, %s::jsonb)",
              (f"{ORG}:{PRETEUSE}", connector, account, json.dumps(meta), json.dumps(side)))


def _services(dsn, connector):
    return _executer(dsn, "SELECT share_services FROM connector_credentials "
                          "WHERE connector = %s", (connector,))[0][0]


def test_la_revision_fige_les_prets_existants_sur_les_services_autorises(live):
    from alembic import command
    from oto_mcp.auth import google as g
    scopes = " ".join(sc for svc in ("gmail", "google_ads") for sc in g.SERVICE_SCOPES[svc])
    _executer(live, "ALTER TABLE connector_credentials DROP COLUMN share_services")
    _ligne(live, "google", "a@exemple.test", {"scopes": scopes},
           [f"user:{PAIR}", "group:3"])
    _ligne(live, "hunter", "", {}, [f"user:{PAIR}"])           # clé entière : pas de liste
    command.stamp(_alembic(), "0059_transcription_key_mode")
    command.upgrade(_alembic(), "0060_services_pretes")
    assert _services(live, "google") == {f"user:{PAIR}": ["gmail", "google_ads"],
                                          "group:3": ["gmail", "google_ads"]}
    assert _services(live, "hunter") == {}
    command.downgrade(_alembic(), "0059_transcription_key_mode")
    assert not _executer(live, "SELECT 1 FROM information_schema.columns WHERE "
                               "table_name = 'connector_credentials' "
                               "AND column_name = 'share_services'")


def test_la_pose_ecrit_la_liste_et_le_retrait_l_efface(live, monkeypatch):
    from oto_mcp import access, credentials_store, db
    from oto_mcp.auth import google as g
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import sharing
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(db, "get_user", lambda sub: {"sub": sub})
    _ligne(live, "google", "a@exemple.test",
           {"scopes": " ".join(g.SERVICE_SCOPES["google_ads"])}, [])
    ctx = ResolvedCtx(sub=PRETEUSE, org_id=ORG)
    out = sharing._lend_instance(ctx, sharing.LendInstanceInput(connector="google_ads",
                                                                to=PAIR))
    assert out["services"] == ["google_ads"]
    ligne = ("member", f"{ORG}:{PRETEUSE}", "google", "a@exemple.test")
    assert credentials_store.services_pretes(*ligne) == {f"user:{PAIR}": ["google_ads"]}
    sharing._lend_instance(ctx, sharing.LendInstanceInput(connector="google", to=PAIR,
                                                          revoke=True))
    assert credentials_store.services_pretes(*ligne) == {}

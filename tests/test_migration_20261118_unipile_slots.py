"""La révision `20261118_1500_unipile_slots` donne l'emplacement à la PK
d'`unipile_accounts` (lot B des plusieurs comptes par canal).

Jouée sur une vraie base remise à la forme d'AVANT (PK `(sub, org_id, provider)`, telle
que la production l'a) : avant, un second compte sur le canal est refusé par la base et
`slots_ouverts()` le dit — mais la (re)connexion ordinaire marche, par l'index unique du
lot A ; après, le second compte entre ; rejouée, elle ne fait rien ; le retour arrière
refuse tant qu'un compte ajouté existe.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_FICHIER = (RACINE / "oto_mcp" / "db" / "migrations" / "versions"
            / "20261118_1500_unipile_slots.py")


def _revision():
    spec = importlib.util.spec_from_file_location("rev_unipile_slots", _FICHIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sql(dsn, sql):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql)


def _pk(dsn) -> set:
    import psycopg
    with psycopg.connect(dsn) as c:
        return {r[0] for r in c.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'unipile_accounts'::regclass AND i.indisprimary")}


def _rouvrir_le_cache():
    from oto_mcp.db import unipile
    unipile._SLOTS_OUVERTS = False


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """La PK de la production d'avant : `(sub, org_id, provider)`, sous son nom d'avant."""
    _sql(pg_module_dsn,
         "ALTER TABLE unipile_accounts DROP CONSTRAINT unipile_accounts_slot_pkey; "
         "ALTER TABLE unipile_accounts ADD CONSTRAINT unipile_accounts_pkey "
         "PRIMARY KEY (sub, org_id, provider)")
    _rouvrir_le_cache()
    return pg_module_dsn


@pytest.fixture
def membre(base_d_avant):
    from oto_mcp import db, org_store
    uniq = uuid.uuid4().hex[:8]
    sub = f"u_{uniq}"
    db.upsert_user(sub)
    org = org_store.create_org(f"o_{uniq}", created_by=sub)
    return {"sub": sub, "org": org, "a": f"acc_a_{uniq}", "b": f"acc_b_{uniq}"}


def test_avant_la_revision(base_d_avant, membre):
    import psycopg
    from oto_mcp import db
    s = membre
    assert _pk(base_d_avant) == {"sub", "org_id", "provider"}
    assert db.slots_ouverts() is False
    # La connexion ordinaire marche sous l'ancienne PK : le code neuf arbitre sur l'index
    # unique à quatre colonnes, qui existe dans les deux états.
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    db.set_unipile_account(s["sub"], s["b"], org_id=s["org"], platform_seat=True)
    assert db.get_unipile_account_id(s["sub"], s["org"]) == s["b"]   # remplacé, comme avant
    # Un second compte, lui, est refusé par la base : d'où la garde `slots_ouverts`.
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True,
                               slot=db.slot_pour_ajout(s["a"]))


def test_apres_la_revision(base_d_avant, membre):
    from oto_mcp import db
    rev = _revision()
    _sql(base_d_avant, rev.UPGRADE)
    _sql(base_d_avant, rev.UPGRADE)          # rejouée : rien à faire, aucune erreur
    assert _pk(base_d_avant) == {"sub", "org_id", "provider", "slot"}
    _rouvrir_le_cache()
    assert db.slots_ouverts() is True
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    db.set_unipile_account(s["sub"], s["b"], org_id=s["org"], platform_seat=True,
                           slot=db.slot_pour_ajout(s["b"]))
    assert len(db.live_unipile_accounts(s["sub"], s["org"])) == 2


def test_le_retour_arriere_refuse_tant_qu_un_compte_ajoute_existe(base_d_avant):
    import psycopg
    rev = _revision()
    with pytest.raises(psycopg.errors.RaiseException):
        _sql(base_d_avant, rev.DOWNGRADE)
    _sql(base_d_avant, "DELETE FROM unipile_accounts WHERE slot <> ''")
    _sql(base_d_avant, rev.DOWNGRADE)
    assert _pk(base_d_avant) == {"sub", "org_id", "provider"}
    _rouvrir_le_cache()

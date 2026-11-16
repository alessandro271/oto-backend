"""Délier un canal, en SQL réel : ce que la suppression du siège chez le fournisseur
lit, et la date que la facturation lit.

Les bancs capacité (`test_unipile_me_capability.py`) doublent `db.*` : ils ne prouvent
pas que la requête « ce compte est-il encore en service dans une autre org ? » regarde
les bonnes lignes. Une erreur ici supprimerait chez le fournisseur un compte qu'une
autre org utilise encore — irréversible."""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def deux_orgs(live):
    from oto_mcp import db, org_store
    uniq = uuid.uuid4().hex[:8]
    sub = f"u_{uniq}"
    db.upsert_user(sub)
    org_a = org_store.create_org(f"a_{uniq}", created_by=sub)
    org_b = org_store.create_org(f"b_{uniq}", created_by=sub)
    compte = f"acc_{uniq}"
    return {"sub": sub, "a": org_a, "b": org_b, "compte": compte}


def test_un_compte_adopte_ailleurs_reste_en_service(deux_orgs):
    from oto_mcp import db
    s = deux_orgs
    for org in (s["a"], s["b"]):
        db.set_unipile_account(s["sub"], s["compte"], org_id=org, platform_seat=True)
    db.clear_unipile_account(s["sub"], s["a"], "LINKEDIN")
    assert db.unipile_account_in_service(s["compte"]) is True
    db.clear_unipile_account(s["sub"], s["b"], "LINKEDIN")
    assert db.unipile_account_in_service(s["compte"]) is False


def test_une_ligne_morte_ne_compte_pas_et_un_compte_inconnu_non_plus(deux_orgs):
    from oto_mcp import db
    s = deux_orgs
    db.set_unipile_account(s["sub"], s["compte"], org_id=s["a"], platform_seat=True)
    db.clear_unipile_account(s["sub"], s["a"], "LINKEDIN")
    assert db.unipile_account_in_service(s["compte"]) is False
    assert db.unipile_account_in_service("acc_" + uuid.uuid4().hex) is False


def test_une_seconde_deconnexion_ne_deplace_pas_la_date(deux_orgs):
    """La facturation lit `disconnected_at` : un DELETE répété ne la réécrit pas."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    s = deux_orgs
    db.set_unipile_account(s["sub"], s["compte"], org_id=s["a"], platform_seat=True)
    db.clear_unipile_account(s["sub"], s["a"], "LINKEDIN")

    def _date():
        with _connect() as conn:
            return conn.execute(
                "SELECT disconnected_at FROM unipile_accounts "
                "WHERE sub = %s AND org_id = %s", (s["sub"], s["a"])).fetchone()[
                    "disconnected_at"]
    premiere = _date()
    assert premiere is not None
    db.clear_unipile_account(s["sub"], s["a"], "LINKEDIN")
    assert _date() == premiere

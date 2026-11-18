"""Plusieurs comptes par canal de messagerie hébergée, en SQL réel.

Les bancs capacité doublent `db.*` : ils ne prouvent ni que la clé de facturation d'un
compte d'avant reste la même à l'octet, ni qu'un compte ajouté occupe un emplacement à
lui (une sous-instance, une connexion facturée), ni qu'un prêt suit l'emplacement prêté
et pas le premier compte du canal venu. Une erreur ici facturerait deux fois, ou ferait
agir un bénéficiaire sous un compte qu'on ne lui a pas prêté."""
from __future__ import annotations

import hashlib
import uuid

import pytest


@pytest.fixture
def membre(live):
    from oto_mcp import db, org_store
    uniq = uuid.uuid4().hex[:8]
    sub = f"u_{uniq}"
    db.upsert_user(sub)
    org = org_store.create_org(f"o_{uniq}", created_by=sub)
    return {"sub": sub, "org": org, "a": f"acc_a_{uniq}", "b": f"acc_b_{uniq}",
            "c": f"acc_c_{uniq}"}


def _ajouter(db, s, compte, org=None):
    return db.set_unipile_account(s["sub"], compte, org_id=org or s["org"],
                                  platform_seat=True, slot=db.slot_pour_ajout(compte))


def test_la_base_neuve_ouvre_les_emplacements(live):
    from oto_mcp import db
    assert db.slots_ouverts() is True


def test_un_seul_compte_garde_la_cle_de_facturation_d_avant(membre):
    """La clé d'une connexion déjà facturée ne change pas : principal = forme d'avant."""
    from oto_mcp import db
    s = membre
    assert db.set_unipile_account(s["sub"], s["a"], org_id=s["org"],
                                  platform_seat=True) == ""
    [c] = db.list_org_unipile_connections(s["org"])
    avant = hashlib.sha256(f"{s['org']}:{s['sub']}:LINKEDIN".encode()).hexdigest()[:24]
    assert c["connection_key"] == avant
    assert db.get_unipile_account_id(s["sub"], s["org"]) == s["a"]


def test_un_compte_ajoute_est_une_connexion_de_plus(membre):
    from oto_mcp import db
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    assert _ajouter(db, s, s["b"]) == s["b"]
    vivants = db.live_unipile_accounts(s["sub"], s["org"])
    assert [v["account_id"] for v in vivants] == [s["a"], s["b"]]   # principal d'abord
    assert db.get_unipile_account_id(s["sub"], s["org"]) == s["a"]
    assert db.count_unipile_accounts_for_org(s["org"]) == 2         # deux sièges
    cles = {c["connection_key"] for c in db.list_org_unipile_connections(s["org"])}
    assert len(cles) == 2


def test_un_meme_compte_n_occupe_jamais_deux_emplacements(membre):
    """Le même identifiant ré-ajouté, ou reconnecté par le chemin ordinaire, reprend
    SON emplacement : jamais deux sièges facturés pour une session."""
    from oto_mcp import db
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    _ajouter(db, s, s["b"])
    assert _ajouter(db, s, s["b"]) == s["b"]
    assert db.set_unipile_account(s["sub"], s["b"], org_id=s["org"],
                                  platform_seat=True) == s["b"]
    assert len(db.live_unipile_accounts(s["sub"], s["org"])) == 2
    assert db.get_unipile_account_id(s["sub"], s["org"]) == s["a"]   # principal intact


def test_deconnecter_un_compte_laisse_les_autres(membre):
    from oto_mcp import db
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    _ajouter(db, s, s["b"])
    assert db.clear_unipile_account(s["sub"], s["org"], "LINKEDIN", account_id=s["b"]) == 1
    assert [v["account_id"] for v in db.live_unipile_accounts(s["sub"], s["org"])] == [s["a"]]
    # Sans `account_id` : le compte servi par défaut, jamais tous.
    _ajouter(db, s, s["c"])
    assert db.clear_unipile_account(s["sub"], s["org"], "LINKEDIN") == 1
    assert [v["account_id"] for v in db.live_unipile_accounts(s["sub"], s["org"])] == [s["c"]]


def test_sans_principal_aucun_compte_ajoute_ne_sert_d_office(membre):
    """Le principal déconnecté, un compte ajouté n'est jamais le défaut, même seul
    (décision du 18/11/2026) : ni servi, ni délié sans être nommé."""
    from oto_mcp import db
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    _ajouter(db, s, s["b"])
    db.clear_unipile_account(s["sub"], s["org"], "LINKEDIN", account_id=s["a"])
    assert db.get_unipile_account_id(s["sub"], s["org"]) is None
    assert db.get_unipile_account(s["sub"], s["org"], "LINKEDIN") is None
    assert db.clear_unipile_account(s["sub"], s["org"], "LINKEDIN") == 0
    _ajouter(db, s, s["c"])
    assert db.get_unipile_account_id(s["sub"], s["org"]) is None
    assert db.clear_unipile_account(s["sub"], s["org"], "LINKEDIN") == 0
    assert len(db.live_unipile_accounts(s["sub"], s["org"])) == 2


def test_chaque_compte_a_sa_sous_instance(membre):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    _ajouter(db, s, s["b"])
    with _connect() as conn:
        comptes = {r["account"] for r in conn.execute(
            "SELECT account FROM connector_instances WHERE owner_type = 'member' "
            "AND owner_id = %s AND connector = 'unipile' AND revoked_at IS NULL",
            (f"{s['org']}:{s['sub']}",)).fetchall()}
    assert comptes == {"canal:LINKEDIN", f"canal:LINKEDIN:{s['b']}"}


def test_un_pret_suit_l_emplacement_prete(membre):
    """Prêter le compte ajouté : le bénéficiaire opère CE compte, pas le principal."""
    from oto_mcp import db
    s = membre
    db.set_unipile_account(s["sub"], s["a"], org_id=s["org"], platform_seat=True)
    _ajouter(db, s, s["b"])
    beneficiaire = f"g_{uuid.uuid4().hex[:8]}"
    db.upsert_user(beneficiaire)
    db.set_account_grant(s["sub"], "LINKEDIN", s["b"], beneficiaire, granted_by=s["sub"],
                         slot=s["b"])
    assert set(db.granted_accounts_for(beneficiaire, "LINKEDIN")) == {s["b"]}
    [recu] = db.list_account_grants_to(beneficiaire)
    assert recu["account_id"] == s["b"] and recu["active"]
    # Un prêt d'avant (sans emplacement) suit le principal, comme toujours.
    db.set_account_grant(s["sub"], "LINKEDIN", s["a"], beneficiaire, granted_by=s["sub"])
    assert set(db.granted_accounts_for(beneficiaire, "LINKEDIN")) == {s["a"]}


def test_une_demande_d_ajout_se_relit(membre):
    from oto_mcp import db
    s = membre
    nonce = uuid.uuid4().hex
    db.create_unipile_pending(nonce, s["sub"], s["org"], "WHATSAPP", platform_seat=True,
                              add_account=True)
    [p] = [x for x in db.list_unipile_pending_for_sub(s["sub"]) if x["nonce"] == nonce]
    assert p["add_account"] is True and p["provider"] == "WHATSAPP"

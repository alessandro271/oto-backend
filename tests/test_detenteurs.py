"""Un refus « réservé à un administrateur » dit QUI le détient — à un membre seulement
(oto#108).

Le cas qui l'a fait naître : la gestionnaire du compte d'un fournisseur, simple membre de
son équipe, régénère la clé d'API et ne peut pas la poser ; le refus ne lui dit ni qui le
peut ni où aller, et le secret finit par circuler par un lien externe pour qu'un
administrateur le recolle. Le refus nomme désormais le rôle, le niveau, et — à un membre
de la même org, jamais à un tiers — les personnes qui le tiennent.

Sans base : `org_store`, `group_store` et `db` sont doublés, comme la CI.
"""
from __future__ import annotations

import pytest

from oto_mcp import db, detenteurs, group_store, org_store, roles
from oto_mcp.capabilities import _authz

_ORG, _EQUIPE = 7, 70
_MEMBRES = {"adm": "org_admin", "adm2": "org_admin", "pause": "org_admin",
            "gestionnaire": "org_member", "chef": "org_member"}
_USERS = {
    "adm": {"name": "Admin Un", "email": "admin1@example.test"},
    "adm2": {"email": "admin2@example.test"},          # aucun nom renseigné
    "pause": {"name": "En Pause", "email": "pause@example.test",
              "suspended_at": "2026-09-01"},
    "chef": {"name": "Chef Equipe", "email": "chef@example.test"},
}


@pytest.fixture(autouse=True)
def _annuaire(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, org_id: org_id == _ORG and sub in _MEMBRES)
    monkeypatch.setattr(org_store, "list_org_members", lambda org_id: [
        {"sub": s, "org_role": r} for s, r in _MEMBRES.items()] if org_id == _ORG else [])
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": _ORG} if gid == _EQUIPE else None)
    monkeypatch.setattr(group_store, "list_group_members", lambda gid: [
        {"sub": "chef", "group_role": "group_admin"},
        {"sub": "gestionnaire", "group_role": "group_member"}] if gid == _EQUIPE else [])
    monkeypatch.setattr(db, "get_user", lambda sub: _USERS.get(sub))


def test_un_membre_recoit_les_administrateurs_nommes_sans_les_comptes_en_pause():
    refus = _authz._refus_org_admin(_ORG, sub="gestionnaire")
    assert refus.status == 403 and refus.code == "forbidden"
    assert f"administrateur de l'org #{_ORG}" in refus.message
    assert "Admin Un, 1 sans nom renseigné." in refus.message
    assert "En Pause" not in refus.message
    # Le NOM seul, jamais l'adresse — ni dans la phrase, ni dans la forme structurée.
    assert "@" not in refus.message and "@" not in repr(refus.details)
    assert refus.details == {
        "required_role": "org_admin", "scope": "org", "org_id": _ORG,
        "holders": [{"name": "Admin Un"}, {"name": None}]}


def test_un_tiers_recoit_le_role_et_le_niveau_jamais_les_personnes():
    """L'annuaire d'une org n'est pas public : un compte qui n'en est pas membre (une
    autre org, un ex-membre) apprend le rôle requis, pas qui le tient."""
    refus = _authz._refus_org_admin(_ORG, sub="etranger")
    assert f"administrateur de l'org #{_ORG}" in refus.message
    assert "@" not in refus.message
    assert "holders" not in refus.details
    assert refus.details["required_role"] == "org_admin"


def test_le_refus_d_equipe_nomme_le_niveau_et_les_chefs_puis_les_admins_d_org():
    refus = _authz._refus_chef_d_equipe(_EQUIPE, sub="gestionnaire")
    assert refus.status == 403 and refus.code == "forbidden"
    assert f"chef de l'équipe #{_EQUIPE}" in refus.message
    assert "administrateur de son org" in refus.message
    assert "Chefs de cette équipe : Chef Equipe." in refus.message
    assert "Administrateurs de son org : Admin Un" in refus.message
    assert "@" not in refus.message and "@" not in repr(refus.details)
    assert refus.details["scope"] == "group" and refus.details["org_id"] == _ORG
    assert refus.details["holders"]["group_admins"] == [{"name": "Chef Equipe"}]


def test_le_refus_d_equipe_ne_nomme_personne_a_un_tiers():
    refus = _authz._refus_chef_d_equipe(_EQUIPE, sub="etranger")
    assert "@" not in refus.message and "holders" not in refus.details


def test_un_palier_sans_personne_joignable_le_dit():
    assert detenteurs.phrase("Chefs de cette équipe", []) == \
        " Chefs de cette équipe : personne de joignable."
    assert detenteurs.phrase("Chefs de cette équipe", None) == ""


def test_la_regle_d_autz_porte_les_detenteurs(monkeypatch):
    """Le chemin réel : la règle `GROUP_ADMIN_OF` d'une clé d'équipe refuse la
    gestionnaire, et son refus lui dit à qui demander la pose."""
    from pydantic import BaseModel
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx

    class _Inp(BaseModel):
        group_id: int

    monkeypatch.setattr(_authz.roles, "can_admin_group", lambda sub, gid: False)
    with pytest.raises(AuthzDenied) as e:
        _authz.GROUP_ADMIN_OF("group_id")(RawCtx(sub="gestionnaire"), _Inp(group_id=_EQUIPE))
    assert "Chef Equipe" in e.value.message


def test_une_option_dit_qui_la_leve_et_ou(monkeypatch):
    from oto_mcp import links
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p:
                        "https://dashboard.example.test/org/billing" if kind == "billing"
                        else None)
    texte = detenteurs.qui_leve_une_option("gestionnaire", _ORG)
    assert "administrateur de cette org" in texte
    assert "https://dashboard.example.test/org/billing" in texte
    assert "équipe de la plateforme" in texte
    assert "facturation de l'org" in texte
    assert "Admin Un" in texte and "admin1@example.test" not in texte
    assert "Admin Un" not in detenteurs.qui_leve_une_option("etranger", _ORG)

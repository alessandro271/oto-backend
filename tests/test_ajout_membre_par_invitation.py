"""`org.member.add` : ajouter quelqu'un à une org, c'est l'inviter.

Règle produit : on n'entre dans une org que par une invitation qu'on accepte. L'ajout
d'un non-membre émet donc une invitation à son adresse (le même geste que
`org.invite.create`) ; la personne n'est membre qu'après l'avoir acceptée, et son org
active ne bouge pas d'ici là. Un membre existant change de rôle tout de suite.

Vrai PostgreSQL : l'appartenance, l'org active et l'invitation en attente vivent dans le
SQL. Le mail et le magic-link, services réseau, sont doublés.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.auth import annuaire
    from oto_mcp.capabilities.orgs import invites
    monkeypatch.setenv("OTO_INVITE_BASE_URL", "https://front.example.test")
    monkeypatch.setattr(annuaire, "magic_url", lambda url, email, **_: url)
    envois: list[str] = []
    monkeypatch.setattr(invites.email_mod, "send_invite_email",
                        lambda to, *a, **kw: envois.append(to) or True)
    u = uuid.uuid4().hex[:8]
    admin, cible = f"admin_{u}", f"cible_{u}"
    db.upsert_user(admin, email=f"{admin}@example.test")
    org = org_store.create_org(f"equipe_{u}", created_by=admin)
    org_store.add_org_member(org, admin, "org_admin")
    email = f"{cible}@example.test"
    db.upsert_user(cible, email=email)
    maison = org_store.ensure_home_org(cible, email=email)   # l'org de son inscription
    return SimpleNamespace(admin=admin, cible=cible, email=email, org=org,
                           maison=maison, envois=envois)


def _ajouter(m, target: str, role: str = "org_member", *, par: str | None = None) -> dict:
    from oto_mcp.capabilities.orgs import members as om
    return om._add_member(SimpleNamespace(sub=par or m.admin),
                          om.AddMemberInput(org_id=m.org, target=target, role=role))


def test_ajouter_un_non_membre_cree_une_invitation_qu_il_accepte(monde):
    from oto_mcp import org_store
    from oto_mcp.capabilities.orgs import invites
    m = monde
    out = _ajouter(m, m.email, "org_admin")
    assert out["status"] == "invited" and out["sub"] is None
    assert out["email"] == m.email and out["role"] == "org_admin"
    assert m.envois == [m.email]
    # Pas membre, et son org active n'a pas bougé.
    assert org_store.get_org_role(m.org, m.cible) is None
    assert org_store.get_active_org(m.cible) == m.maison
    assert org_store.find_pending_invitation(m.org, m.email)

    token = out["invite_url"].rsplit("/invitation/", 1)[1]
    acceptee = invites._invite_accept(SimpleNamespace(sub=m.cible),
                                      invites.InviteAcceptInput(token=token))
    assert acceptee["org_id"] == m.org
    assert org_store.get_org_role(m.org, m.cible) == "org_admin"


def test_une_deuxieme_demande_ne_cree_pas_une_deuxieme_invitation(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _ajouter(m, m.email)
    with pytest.raises(AuthzDenied) as e:
        _ajouter(m, m.email)
    assert (e.value.status, e.value.code) == (409, "already_invited")


def test_un_non_membre_designe_par_son_sub_se_designe_par_son_adresse(monde):
    from oto_mcp import org_store
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    with pytest.raises(AuthzDenied) as e:
        _ajouter(m, m.cible)
    assert (e.value.status, e.value.code) == (400, "invite_by_email")
    assert org_store.get_org_role(m.org, m.cible) is None
    assert org_store.get_active_org(m.cible) == m.maison
    assert m.envois == []


@pytest.mark.parametrize("par_adresse", [True, False], ids=["email", "sub"])
def test_le_role_d_un_membre_existant_change_tout_de_suite(monde, par_adresse):
    from oto_mcp import org_store
    m = monde
    org_store.add_org_member(m.org, m.cible, "org_member")
    out = _ajouter(m, m.email if par_adresse else m.cible, "org_admin")
    assert out == {"ok": True, "org_id": m.org, "sub": m.cible, "role": "org_admin",
                   "status": "member"}
    assert org_store.get_org_role(m.org, m.cible) == "org_admin"
    assert m.envois == []


def test_l_appelant_qui_se_designe_lui_meme_rejoint_directement(monde):
    """L'opérateur plateforme qui rejoint une org par escalade `ORG_ADMIN_OF` : c'est
    son propre geste, il n'a personne à qui demander."""
    from oto_mcp import db, org_store
    m = monde
    operateur = f"op_{uuid.uuid4().hex[:8]}"
    db.upsert_user(operateur, email=f"{operateur}@example.test")
    out = _ajouter(m, operateur, "org_admin", par=operateur)
    assert out["status"] == "member"
    assert org_store.get_org_role(m.org, operateur) == "org_admin"

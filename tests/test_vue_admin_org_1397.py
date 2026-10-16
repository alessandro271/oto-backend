"""Ce qu'un admin d'org voit de son org, depuis un agent (signal 1397).

`oto_group op=get` (membres + comptes partagés ou prêtés à l'équipe), `op=list_invites`
sur `oto_org` et `oto_group`, et `oto_org op=list_loans` : les prêts actifs de l'org.
Les deux premières branchent les handlers que sert déjà la console ; la troisième est
neuve. Les autorisations restent celles du serveur. Toutes les valeurs sont fictives.
"""
from datetime import datetime, timezone

import pytest

from oto_mcp import credentials_store
from oto_mcp.capabilities import org_console
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.connectors import sharing
from oto_mcp.capabilities.registry import CAPABILITIES

ORG = 7
CTX = ResolvedCtx(sub="admin", org_id=ORG)


def _cap(key):
    return next(c for c in CAPABILITIES if c.key == key)


@pytest.mark.parametrize("cle,inp,seam", [
    ("org.console", org_console.OrgInput(op="list_loans", org_id=ORG), "is_org_admin"),
    ("org.console", org_console.OrgInput(op="list_invites", org_id=ORG), "is_org_admin"),
    ("org.loans.list", sharing.OrgLoansInput(org_id=ORG), "is_org_admin"),
    ("group.console", org_console.GroupInput(op="list_invites", group_id=3), "can_admin_group"),
    ("group.console", org_console.GroupInput(op="get", group_id=3), "can_read_group"),
])
def test_les_ops_neuves_sont_gardees_cote_serveur(monkeypatch, cle, inp, seam):
    """Les invitations et les prêts de l'org = l'administrer ; celles d'une équipe = la
    mener ; lire une équipe = en être membre (un admin d'org y passe par l'escalade).
    Les mêmes règles que les capacités que la console appelle déjà."""
    from oto_mcp import roles
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(roles, seam, lambda *a, **k: False)
    # Le refus nomme les admins à joindre (lecture de base) : seule sa forme compte ici.
    for refus_nomme in ("_refus_org_admin", "_refus_chef_d_equipe"):
        monkeypatch.setattr(_authz, refus_nomme,
                            lambda *a, **k: AuthzDenied(403, "forbidden", "refus"))
    with pytest.raises(AuthzDenied) as refus:
        _cap(cle).authz(RawCtx(sub="intrus"), inp)
    assert refus.value.status == 403


def test_group_get_et_list_invites_branchent_les_handlers_de_la_console(monkeypatch):
    vus = []
    monkeypatch.setattr(org_console.groups, "_group_detail",
                        lambda ctx, inp: vus.append(("get", inp.group_id)) or {"ok": 1})
    monkeypatch.setattr(org_console.groups_invites, "_invite_list",
                        lambda ctx, inp: vus.append(("g_inv", inp.group_id)) or {"ok": 2})
    monkeypatch.setattr(org_console.orgs_invites, "_invite_list",
                        lambda ctx, inp: vus.append(("o_inv", inp.org_id)) or {"ok": 3})
    assert org_console._group(CTX, org_console.GroupInput(op="get", group_id=3)) == {"ok": 1}
    assert org_console._group(CTX, org_console.GroupInput(op="list_invites", group_id=3)) == {"ok": 2}
    assert org_console._org(CTX, org_console.OrgInput(op="list_invites", org_id=ORG)) == {"ok": 3}
    assert vus == [("get", 3), ("g_inv", 3), ("o_inv", ORG)]


@pytest.fixture
def parc(monkeypatch):
    lignes = [
        {"entity_id": f"{ORG}:alice", "connector": "google", "account": "alice@exemple.test",
         "share_side": ["user:bob", "group:3"],
         "share_services": {"user:bob": ["google_ads"], "group:3": ["drive", "gmail"]}},
        {"entity_id": f"{ORG}:carol", "connector": "zoho", "account": "",
         "share_side": [f"org:{ORG}"], "share_services": {}},
    ]
    monkeypatch.setattr(credentials_store, "prets_de_l_org",
                        lambda org: lignes if org == ORG else [])
    pose = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(sharing.loan_events, "dernieres_poses", lambda ls: {
        (credentials_store.MEMBER, f"{ORG}:alice", "google", "alice@exemple.test",
         "user:bob"): pose})
    monkeypatch.setattr(sharing.prets, "libelles_preteurs",
                        lambda subs: {s: f"{s}@exemple.test" for s in subs})
    monkeypatch.setattr(sharing.group_store, "get_group",
                        lambda gid: {"id": gid, "name": "Ventes"})
    return pose


def test_list_loans_dit_qui_prete_quoi_a_qui_et_depuis_quand(parc):
    out = org_console._org(CTX, org_console.OrgInput(op="list_loans", org_id=ORG))
    assert out["count"] == 3
    a_bob, a_equipe, a_org = out["loans"]
    assert (a_bob["lender"], a_bob["connector"], a_bob["account"], a_bob["to"],
            a_bob["to_sub"], a_bob["to_label"]) == (
        "alice@exemple.test", "google", "alice@exemple.test", "user", "bob",
        "bob@exemple.test")
    assert a_bob["since"] == parc.isoformat()
    # Les services prêtés, par emprunteur ; une clé sans services se prête entière.
    assert (a_bob["services"], a_equipe["services"], a_org["services"]) == (
        ["google_ads"], ["drive", "gmail"], None)
    assert (a_equipe["to"], a_equipe["to_group"], a_equipe["to_label"]) == ("group", 3, "Ventes")
    # Prêté avant que le journal n'existe : la date n'est pas connue, et ça se dit.
    assert a_equipe["since"] is None
    assert (a_org["lender_sub"], a_org["to"], a_org["since"]) == ("carol", "org", None)
    sharing.OrgLoans.model_validate(out)


def test_list_loans_d_une_org_sans_pret_est_vide(parc):
    out = sharing._list_org_loans(CTX, sharing.OrgLoansInput(org_id=99))
    assert out == {"org_id": 99, "loans": [], "count": 0}

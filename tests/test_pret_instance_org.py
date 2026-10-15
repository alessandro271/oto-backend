"""Lending a member instance to the WHOLE current org (`to_org`, `share_side` `org:<id>`).

The pose writes the org's scope, the response says the complete state (`lent_to_org`),
the gesture is journaled only when it changes something, and the same refusals as a team
loan apply: exactly one borrower, a carrier nothing would serve, a personal key the org
forbids. All values are fictitious.
"""
import pytest

from oto_mcp import credentials_store
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import sharing

ORG, LENDER = 7, "lender"
CTX = ResolvedCtx(sub=LENDER, org_id=ORG)


@pytest.fixture
def lend(monkeypatch):
    state = {"side": ["user:peer"], "journal": []}
    monkeypatch.setattr(sharing.access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": "filiale-a"}])
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda *a, **k: (None, list(state["side"])))

    def ecrire(et, eid, con, acc="", *, share_side=None, journal=(), **k):
        state["side"] = list(share_side)
        state["journal"] += [{**g, "entity": (et, eid, con, acc)} for g in journal]
        return True
    monkeypatch.setattr(credentials_store, "set_instance_sharing", ecrire)
    return state


def _preter(**kw):
    return sharing._lend_instance(CTX, sharing.LendInstanceInput(connector="zoho", **kw))


def test_lend_to_the_org_writes_its_scope_and_says_the_whole_state(lend):
    out = _preter(to_org=True)
    assert lend["side"] == ["user:peer", f"org:{ORG}"]
    assert (out["lent_to_org"], out["lent_to"], out["lent_to_groups"]) == (True, ["peer"], [])
    assert "`_account=filiale-a`" in out["note"]
    assert lend["journal"] == [{"actor_sub": LENDER, "org_id": ORG, "target": f"org:{ORG}",
                                "action": "lend",
                                "entity": (credentials_store.MEMBER, f"{ORG}:{LENDER}",
                                           "zoho", "filiale-a")}]


def test_revoke_the_org_loan_is_journaled_and_a_repeat_is_not(lend):
    lend["side"] = [f"org:{ORG}"]
    _preter(to_org=True)                                   # already lent: nothing changes
    assert lend["journal"] == []
    out = _preter(to_org=True, revoke=True)
    assert lend["side"] == [] and out["lent_to_org"] is False
    assert [(g["target"], g["action"]) for g in lend["journal"]] == [(f"org:{ORG}", "revoke")]


@pytest.mark.parametrize("kw", [{"to": "peer", "to_org": True},
                                {"to_group": 3, "to_org": True}])
def test_to_org_excludes_the_other_borrowers(lend, kw):
    with pytest.raises(AuthzDenied) as e:
        _preter(**kw)
    assert e.value.code == "one_borrower"
    assert lend["journal"] == []


def test_a_carrier_nothing_serves_to_the_org_is_refused_at_the_pose(lend, monkeypatch):
    monkeypatch.setattr(sharing.providers, "is_org_shareable", lambda c: False)
    with pytest.raises(AuthzDenied) as e:
        _preter(to_org=True)
    assert e.value.code == "org_loan_not_served"
    assert lend["side"] == ["user:peer"] and lend["journal"] == []


def test_a_personal_key_the_org_forbids_is_not_lent_to_the_org(lend, monkeypatch):
    from oto_mcp import org_store
    from oto_mcp.connectors import cle_perso
    monkeypatch.setattr(cle_perso, "interdite", lambda org, con: (org, con) == (ORG, "zoho"))
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"name": "Acme"})
    with pytest.raises(AuthzDenied) as e:
        _preter(to_org=True)
    assert e.value.code == "personal_key_forbidden"
    assert lend["journal"] == []


def test_oto_instance_lend_passes_to_org_through(lend):
    import asyncio

    from oto_mcp.capabilities.connectors import console
    out = asyncio.run(console._instance(CTX, console.InstanceInput(
        op="lend", connector="zoho", to_org=True)))
    assert out["lent_to_org"] is True

"""Un compte prêté à TOUTE L'ORG (`share_side` `org:<id>`), sur la cascade (palier org).

Même règle qu'un prêt à une équipe : il ne sert les membres de l'org que si l'appel le
NOMME (`_account=`) ; sans nom, le compte de l'org elle-même sert, sinon refus
`account_required` qui le nomme avec sa prêteuse. Le prêt servi est la ligne de la
PRÊTEUSE (trace, écho `lent_via_org`). Il cesse avec sa prêteuse (départ, pause), et ne
sert jamais qui n'est pas membre de l'org. Seams du coffre doublés, valeurs fictives.
"""
import pytest

from oto_mcp import (access, account_suspension, credentials_store, group_store, org_store,
                     roles, session_org)
from oto_mcp.mcp_errors import McpError

ORG, MOI, PRETEUSE = 7, "u1", "preteuse"
CHEZ_PRETEUSE = f"{ORG}:{PRETEUSE}"


class _Con:
    auth_multi_account = True
    name = "serper"
    auth_modes = ("byo", "byo_org")


@pytest.fixture
def coffre(monkeypatch):
    """MOI, membre de l'org ORG sans équipe active ; la prêteuse prête `jane` à l'org."""
    etat = {"multi": True, "org": {}, "membres": {MOI, PRETEUSE}, "en_pause": set(),
            "prets": {("jane", CHEZ_PRETEUSE): "K-PRET"}}
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov, project_id=None: None)
    monkeypatch.setattr(access, "_is_multi_account", lambda p, o=None: etat["multi"])
    monkeypatch.setattr(access.providers, "connector_for_provider", lambda p: _Con())
    monkeypatch.setattr(access.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"serper"})
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    for indice in ("_revoked_hint", "_reachable_hint"):
        monkeypatch.setattr(access, indice, lambda *a, **k: "")
    monkeypatch.setattr(access.db, "emails_by_subs",
                        lambda subs: {PRETEUSE: "preteuse@exemple.test"})
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid, "name": "Acme"})
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org=None: [])
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, org: org == ORG and sub in etat["membres"])
    monkeypatch.setattr(account_suspension, "refus_preteur",
                        lambda sub, quoi: "en pause" if sub in etat["en_pause"] else None)
    monkeypatch.setattr(credentials_store, "list_shared_with", lambda scopes: [
        {"entity_type": credentials_store.MEMBER, "entity_id": eid, "connector": "serper",
         "account": acc, "meta": {}}
        for acc, eid in etat["prets"]] if scopes == [f"org:{ORG}"] else [])
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, con, acc="": (None, [f"org:{ORG}"]))
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": a, "meta": {}} for a in (etat["org"] if (et, eid) == ("org", str(ORG))
                                             else {})])
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, con, account=None: bool(
                            etat["org"] and (et, eid) == ("org", str(ORG))))

    def get(et, eid, con, account=""):
        if et == credentials_store.MEMBER:
            return etat["prets"].get((account, eid))
        return etat["org"].get(account) if (et, eid) == ("org", str(ORG)) else None
    monkeypatch.setattr(credentials_store, "get_credential", get)
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov, account="": etat["org"].get(account))
    return etat


def _resoudre(account=None, sub=MOI):
    return access.resolve_credential("serper", sub=sub, account=account,
                                     emit_on_failure=False)


def test_nomme_le_pret_a_l_org_sert_et_la_ligne_servie_est_celle_de_la_preteuse(coffre):
    tok = session_org.set_call_trace({})
    try:
        rc = _resoudre(account="jane")
        trace = session_org.current_call_trace()
    finally:
        session_org.reset_call_trace(tok)
    assert (rc.key, rc.account, rc.mode) == ("K-PRET", "jane", "org")
    assert (rc.entity_type, rc.entity_id) == (credentials_store.MEMBER, CHEZ_PRETEUSE)
    assert trace["credential_row"] == (credentials_store.MEMBER, CHEZ_PRETEUSE, "serper",
                                       "jane")
    assert access.rappel_du_compte(MOI, "jane", trace["credential_row"]) == {
        "_account": "jane", "lent_by": "preteuse@exemple.test",
        "lent_via_org": {"id": ORG, "name": "Acme"}}


def test_sans_nom_le_pret_a_l_org_ne_sert_pas_et_le_refus_le_nomme(coffre):
    with pytest.raises(McpError) as e:
        _resoudre()
    err = e.value.error
    assert err.data["code"] == "account_required"
    assert "`jane` (lent by preteuse@exemple.test to the whole org Acme)" in err.message
    assert err.data["lent_accounts"] == [{"account": "jane",
                                          "lent_by": "preteuse@exemple.test",
                                          "via_org": {"id": ORG, "name": "Acme"}}]


def test_sans_nom_le_compte_de_l_org_elle_meme_sert(coffre):
    coffre["org"] = {"principal": "K-ORG"}
    rc = _resoudre()
    assert (rc.key, rc.entity_type, rc.entity_id) == ("K-ORG", "org", str(ORG))


def test_un_non_membre_de_l_org_n_est_jamais_servi(coffre):
    coffre["membres"].discard(MOI)
    assert access.prets_de_l_org(ORG, "serper", MOI) == []
    with pytest.raises(McpError) as e:
        _resoudre(account="jane")
    assert "`jane` not found" in e.value.error.message


@pytest.mark.parametrize("laps", [
    lambda c: c["membres"].discard(PRETEUSE),       # la prêteuse a quitté l'org
    lambda c: c["en_pause"].add(PRETEUSE),          # la prêteuse est en pause
])
def test_le_pret_cesse_avec_sa_preteuse(coffre, laps):
    laps(coffre)
    with pytest.raises(McpError) as e:
        _resoudre(account="jane")
    assert "`jane` not found" in e.value.error.message


def test_la_preteuse_ne_s_emprunte_pas_sa_propre_ligne(coffre):
    assert access.prets_de_l_org(ORG, "serper", PRETEUSE) == []


def test_le_pret_a_l_org_entre_dans_la_portee_commune_des_prets(coffre):
    assert [(c["account"], c["via_org"]) for c in access.prets_de(MOI, "serper")] == [
        ("jane", {"id": ORG, "name": "Acme"})]

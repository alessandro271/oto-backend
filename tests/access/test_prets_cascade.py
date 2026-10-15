"""La règle du compte prêté, branchée sur la CASCADE (palier équipe de `walk_cascade`).

Un compte prêté à une équipe (`share_side` `group:<id>`) ne sert ses membres que si
l'appel le NOMME (`_account=`). Sans nom : le compte de l'équipe elle-même, sinon celui
de l'org ; si seuls des prêts auraient servi, refus `account_required` qui les nomme —
jamais la clé plateforme à leur place. Le prêt servi est la ligne du PRÊTEUR : la trace,
l'écho et une rotation de jeton la nomment. Seams du coffre doublés, valeurs fictives.
"""
import pytest

from oto_mcp import (access, account_suspension, credentials_store, group_store, roles,
                     session_org)
from oto_mcp.mcp_errors import McpError

ORG, GID, MOI, PRETEUSE = 7, 3, "u1", "preteuse"
CHEZ_PRETEUSE = f"{ORG}:{PRETEUSE}"


class _Con:
    auth_multi_account = True
    name = "serper"

    def __init__(self, modes):
        self.auth_modes = modes


@pytest.fixture
def coffre(monkeypatch):
    """Une équipe GID de l'org ORG, active pour MOI ; la prêteuse lui prête `jane`."""
    etat = {"multi": True, "modes": ("byo", "byo_org"), "equipe": {}, "org": {},
            "prets": {("jane", CHEZ_PRETEUSE): "K-PRET"}}
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: GID)
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov, project_id=None: None)
    monkeypatch.setattr(access, "_is_multi_account", lambda p, o=None: etat["multi"])
    monkeypatch.setattr(access.providers, "connector_for_provider",
                        lambda p: _Con(etat["modes"]))
    monkeypatch.setattr(access.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"serper"})
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    for indice in ("_revoked_hint", "_reachable_hint"):
        monkeypatch.setattr(access, indice, lambda *a, **k: "")
    monkeypatch.setattr(access.db, "emails_by_subs",
                        lambda subs: {PRETEUSE: "preteuse@exemple.test"})
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": ORG, "name": "Ventes"})
    monkeypatch.setattr(group_store, "is_group_member", lambda sub, gid: sub == MOI)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == ORG)
    monkeypatch.setattr(account_suspension, "refus_preteur", lambda *a, **k: None)
    monkeypatch.setattr(credentials_store, "list_shared_with", lambda scopes: [
        {"entity_type": credentials_store.MEMBER, "entity_id": eid, "connector": "serper",
         "account": acc, "meta": {}}
        for acc, eid in etat["prets"]] if scopes == [f"group:{GID}"] else [])
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, con, acc="": (None, [f"group:{GID}"]))

    def lignes(et, eid):
        if et == "group" and eid == str(GID):
            return etat["equipe"]
        if et == "org" and eid == str(ORG):
            return etat["org"]
        return {}
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": a, "meta": {}} for a in lignes(et, eid)])
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, con, account=None: bool(lignes(et, eid)))

    def get(et, eid, con, account=""):
        if et == credentials_store.MEMBER:
            return etat["prets"].get((account, eid))
        return lignes(et, eid).get(account)
    monkeypatch.setattr(credentials_store, "get_credential", get)
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov, account="": etat["org"].get(account))
    return etat


def _resoudre(account=None):
    return access.resolve_credential("serper", sub=MOI, account=account,
                                     emit_on_failure=False)


def test_sans_nom_un_pret_a_l_equipe_ne_sert_pas_et_le_refus_le_nomme(coffre):
    with pytest.raises(McpError) as e:
        _resoudre()
    err = e.value.error
    assert err.data["code"] == "account_required"
    assert "`jane` (lent by preteuse@exemple.test via the team Ventes)" in err.message
    assert err.data["lent_accounts"] == [{"account": "jane",
                                          "lent_by": "preteuse@exemple.test",
                                          "via_group": {"id": GID, "name": "Ventes"}}]


def test_nomme_le_pret_sert_et_la_ligne_servie_est_celle_de_la_preteuse(coffre):
    tok = session_org.set_call_trace({})
    try:
        rc = _resoudre(account="jane")
        trace = session_org.current_call_trace()
    finally:
        session_org.reset_call_trace(tok)
    assert (rc.key, rc.account, rc.mode) == ("K-PRET", "jane", "group")
    assert (rc.entity_type, rc.entity_id) == (credentials_store.MEMBER, CHEZ_PRETEUSE)
    assert trace["credential_row"] == (credentials_store.MEMBER, CHEZ_PRETEUSE, "serper",
                                       "jane")
    assert trace["instance"] == f"member:{ORG}:{PRETEUSE}:serper:jane"
    assert access.rappel_du_compte(MOI, "jane", trace["credential_row"]) == {
        "_account": "jane", "lent_by": "preteuse@exemple.test",
        "lent_via_group": {"id": GID, "name": "Ventes"}}


def test_nomme_par_l_axe_account_comme_par_le_parametre(coffre):
    tok = session_org.set_call_account("jane")
    try:
        assert _resoudre().key == "K-PRET"
    finally:
        session_org.reset_call_account(tok)


def test_sans_nom_le_compte_de_l_equipe_elle_meme_sert(coffre):
    coffre["equipe"] = {"siege": "K-EQUIPE"}
    rc = _resoudre()
    assert (rc.key, rc.entity_type, rc.entity_id) == ("K-EQUIPE", "group", str(GID))


def test_sans_nom_le_compte_de_l_org_sert(coffre):
    coffre["org"] = {"principal": "K-ORG"}
    rc = _resoudre()
    assert (rc.key, rc.mode) == ("K-ORG", "org")


def test_sans_nom_jamais_la_cle_plateforme_a_la_place_du_pret(coffre, monkeypatch):
    coffre["modes"] = ("byo", "byo_org", "platform")
    monkeypatch.setattr(access, "_resolve_platform_grant",
                        lambda s, p, o: {"label": "commune", "secret": "K-PLATEFORME"})
    with pytest.raises(McpError) as e:
        _resoudre()
    assert e.value.error.data["code"] == "account_required"


def test_un_connecteur_mono_compte_designe_le_pret_par_sa_ref(coffre):
    coffre["multi"] = False
    coffre["prets"] = {("", CHEZ_PRETEUSE): "K-PRET"}
    with pytest.raises(McpError) as e:
        _resoudre()
    err = e.value.error
    ref = f"member:{ORG}:{PRETEUSE}:serper"
    assert f"`_instance=` with one of `{ref}`" in err.message
    assert err.data["lent_accounts"][0]["instance"] == ref

"""Le connecteur `linkedin_ads` — connexion OAuth LinkedIn de la personne (code
d'autorisation, application plateforme, refresh token ou jeton d'accès seul) et l'outil
de lecture.

⚠️ Le cœur est MOQUÉ à sa frontière (`oto.tools.linkedin_ads` et son `auth` posés dans
`sys.modules`), le coffre et les réglages sont des faux en mémoire : aucun appel réel à
LinkedIn ni à la base. La RÉSOLUTION du credential, elle, est la vraie
(`access.resolve_credential`). Vérifié : le registre, l'application absente nommée, le
flux hébergé (state signé à usage unique, refus LinkedIn lus comme refus), le retour de
connexion (les deux formes de ligne, identité facultative), le renouvellement (cache,
verrou, concurrence, autorisation morte ou expirée MARQUÉE et jamais purgée, application
refusée sans marque), et côté outil : une seule lecture, la correspondance op → méthode,
les arguments refusés ou requis, la vue resserrée d'une campagne, les bornes d'un
rapport, la garde par personne, le 401 rejoué une fois, le 429 jamais rejoué.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

CONNECTEUR = "linkedin_ads"
ORG = 42
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
_RETOUR = "https://mcp.exemple.test/api/linkedin_ads/oauth/callback"
_APP = {"client_id": "id-de-l-application", "client_secret": "secret-de-l-application"}
_COMPTE = "502840441"


def _grant(access="AT1", refresh="RT1", expires_in=5184000, refresh_in=31536000):
    return types.SimpleNamespace(access_token=access, refresh_token=refresh,
                                 expires_in=expires_in,
                                 refresh_token_expires_in=refresh_in if refresh else 0,
                                 scope="r_ads,r_ads_reporting")


def _faux_coeur():
    mod = types.ModuleType("oto.tools.linkedin_ads")
    auth = types.ModuleType("oto.tools.linkedin_ads.auth")

    class LinkedInAdsAuthError(ValueError):
        def __init__(self, message, *, status_code=401, code=None):
            super().__init__(message)
            self.status_code, self.code = status_code, code

    class LinkedInAdsGrantExpired(LinkedInAdsAuthError):
        pass

    auth.LinkedInAdsAuthError = LinkedInAdsAuthError
    auth.LinkedInAdsGrantExpired = LinkedInAdsGrantExpired
    auth.SCOPES = ("r_ads", "r_ads_reporting")
    auth.authorize_url = MagicMock(return_value="https://www.linkedin.test/oauth?x=1")
    auth.exchange_code = MagicMock(return_value=_grant())
    auth.refresh = MagicMock(return_value=_grant("AT2", "RT1"))
    mod.auth = auth
    mod.LinkedInAdsClient = MagicMock(name="LinkedInAdsClient")
    mod.LinkedInAdsClient.return_value.list_account_roles.return_value = {
        "elements": [{"account": f"urn:li:sponsoredAccount:{_COMPTE}", "role": "VIEWER",
                      "user": "urn:li:person:abc"}]}
    return mod


class _Coffre:
    def __init__(self):
        self.lignes: dict[tuple, dict] = {}

    def poser(self, secret, meta=None):
        self.lignes[("member", MEMBRE, "")] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": SUB,
            "set_at": "2026-11-17T00:00:00Z"}

    def get_with_meta(self, entity_type, entity_id, connector, account=""):
        assert connector == CONNECTEUR
        ligne = self.lignes.get((entity_type, entity_id, account))
        return {**ligne, "meta": dict(ligne["meta"])} if ligne else None

    def get(self, entity_type, entity_id, connector, account=""):
        ligne = self.get_with_meta(entity_type, entity_id, connector, account)
        return ligne["secret"] if ligne else None

    def set(self, entity_type, entity_id, connector, secret, set_by=None,
            meta=None, conn=None, account="", expected_version=None):
        assert connector == CONNECTEUR
        self.lignes[(entity_type, entity_id, account)] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": set_by,
            "set_at": "2026-11-17T01:00:00Z", "conn": conn}

    def list_credentials(self, entity_type, entity_id):
        return [{"connector": CONNECTEUR, "account": a, "secret_kind": "oauth",
                 "set_by": l["set_by"], "set_at": l["set_at"], "meta": dict(l["meta"])}
                for (et, eid, a), l in sorted(self.lignes.items())
                if (et, eid) == (entity_type, entity_id)]

    def list_accounts(self, entity_type, entity_id, connector):
        return [{"account": a, "meta": dict(l["meta"]), "set_at": l["set_at"]}
                for (et, eid, a), l in sorted(self.lignes.items())
                if (et, eid) == (entity_type, entity_id)]

    def update_meta(self, entity_type, entity_id, connector, account, patch, conn=None):
        ligne = self.lignes.get((entity_type, entity_id, account))
        if ligne is None:
            return False
        ligne["meta"].update(patch)
        return True


@pytest.fixture
def env(monkeypatch):
    from oto_mcp import access, credentials_store, db, roles
    from oto_mcp.auth import linkedin_ads as li_auth
    from oto_mcp.connectors import activation, cardinality
    from oto_mcp.db import connector_settings as store

    mod = _faux_coeur()
    monkeypatch.setitem(sys.modules, "oto.tools.linkedin_ads", mod)
    monkeypatch.setitem(sys.modules, "oto.tools.linkedin_ads.auth", mod.auth)
    reglages = dict(_APP)
    monkeypatch.setattr(store, "list_connector_settings", lambda *a, **k: [
        {"scope_type": "platform", "scope_id": "platform", "connector": CONNECTEUR,
         "key": k_, "value": v} for k_, v in reglages.items()])
    coffre = _Coffre()
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", coffre.get_with_meta)
    monkeypatch.setattr(credentials_store, "get_credential", coffre.get)
    monkeypatch.setattr(credentials_store, "set_credential", coffre.set)
    monkeypatch.setattr(credentials_store, "list_accounts", coffre.list_accounts)
    monkeypatch.setattr(credentials_store, "list_credentials", coffre.list_credentials)
    monkeypatch.setattr(credentials_store, "update_meta", coffre.update_meta)
    monkeypatch.setattr(db, "member_instance_suspended", lambda *a, **k: False)
    monkeypatch.setattr(db, "insert_tool_call", lambda *a, **k: None)
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda p, project_id=None: None)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(cardinality, "_OVERRIDES", {})
    monkeypatch.setattr(cardinality, "_LOADED", True)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.exemple.test")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-signature-de-test")
    monkeypatch.setattr(li_auth, "_JETONS", {})
    verrou = threading.Lock()
    prises: list = []
    conn = object()

    @contextlib.contextmanager
    def _verrou_ligne(ligne):
        with verrou:
            prises.append(ligne)
            yield conn
    monkeypatch.setattr(li_auth, "_verrou_ligne", _verrou_ligne)
    consommes: set = set()

    def _consume(aud, jti):
        if (aud, jti) in consommes:
            return False
        consommes.add((aud, jti))
        return True
    monkeypatch.setattr(db, "consume_state_jti", _consume)
    membres = {(SUB, ORG)}
    coupes: set = set()
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: (sub, org) in membres)
    monkeypatch.setattr(activation, "cran_qui_coupe",
                        lambda c, org=None, group=None: "org" if (c, org) in coupes else None)
    return types.SimpleNamespace(coeur=mod, coffre=coffre, auth=li_auth, reglages=reglages,
                                 prises=prises, conn=conn, membres=membres, coupes=coupes)


# ── Registre ────────────────────────────────────────────────────────────────

def test_registre():
    c = providers.REGISTRY[CONNECTEUR]
    assert c.secret_kind == "oauth" and c.auth_modes == frozenset({"byo_user"})
    assert c.personal_session
    assert c.publisher_name
    assert not c.credential_fields
    assert c.doc_sections, "la fiche doit être servie depuis son markdown"


def test_le_namespace_se_resout_au_connecteur_et_pas_a_linkedin():
    from oto_mcp import tool_visibility

    assert tool_visibility.namespace_of("linkedin_ads") == "linkedin_ads"
    assert providers.connector_for_namespace("linkedin_ads").name == CONNECTEUR


# ── Application plateforme ──────────────────────────────────────────────────

def test_application_absente_nommee_au_depart_et_sur_la_carte(env):
    from oto_mcp.capabilities._types import AuthzDenied

    del env.reglages["client_secret"]
    with pytest.raises(RuntimeError, match="missing client_secret"):
        env.auth.app()
    assert not env.auth.app_disponible(SUB)
    with pytest.raises(AuthzDenied) as e:
        env.auth._start_flow(types.SimpleNamespace(sub=SUB), {})
    assert "client_secret" in str(e.value)
    assert env.auth._etape_manquante(SUB, None, None, {}) == (
        "LinkedIn app to be configured by the operator")
    env.coeur.auth.authorize_url.assert_not_called()


# ── Flux hébergé ────────────────────────────────────────────────────────────

def test_url_de_retour_derivee_de_l_environnement(env):
    from oto_mcp.connectors import flow as connector_flow

    assert connector_flow.supports(CONNECTEUR)
    assert connector_flow.callback_url(CONNECTEUR) == _RETOUR


def _etat(env):
    return env.auth.make_state(SUB, ORG, "")


def test_state_ne_vaut_que_pour_ce_flux_et_porte_son_jti(env):
    from oto_mcp.auth import flow as oauth_flow

    lu = env.auth.verify_state(_etat(env))
    assert (lu["sub"], lu["org"], lu["app"]) == (SUB, ORG, "")
    assert lu["jti"]
    assert env.auth.verify_state(oauth_flow.sign_state(
        "jefacture", {"sub": SUB, "org": ORG, "jti": "j"})) is None
    assert env.auth.verify_state(oauth_flow.sign_state(
        "linkedin_ads", {"sub": SUB, "org": ORG})) is None  # sans jti : refusé


def test_le_dialogue_porte_le_client_de_l_application_et_le_retour(env):
    env.auth.build_auth_url(SUB, "")
    client_id, retour, etat = env.coeur.auth.authorize_url.call_args.args
    assert client_id == "id-de-l-application" and retour == _RETOUR
    assert env.auth.verify_state(etat)["sub"] == SUB


# ── Retour de connexion ─────────────────────────────────────────────────────

def _callback(query: dict):
    from starlette.requests import Request

    from oto_mcp.api import linkedin_ads as api_li

    route = api_li.make_routes(None, None, None, None, None)[0]
    qs = "&".join(f"{k}={v}" for k, v in query.items()).encode()
    req = Request({"type": "http", "method": "GET", "path": route.path,
                   "query_string": qs, "headers": []})
    return asyncio.run(route.endpoint(req))


def test_retour_range_le_refresh_token_et_l_identite(env):
    resp = _callback({"code": "le-code", "state": _etat(env)})
    assert resp.status_code == 302 and "connected" in resp.headers["location"]
    assert env.coeur.auth.exchange_code.call_args.args == (
        "id-de-l-application", "secret-de-l-application", "le-code", _RETOUR)
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT1"
    meta = ligne["meta"]
    assert meta["token_shape"] == "refresh" and meta["member_urn"] == "urn:li:person:abc"
    assert meta["access_expires_at"] and meta["refresh_expires_at"] and meta["connected_at"]
    assert ligne["conn"] is env.conn, "la pose se fait sous le verrou de la ligne"
    # le jeton d'accès de l'échange sert tout de suite, sans renouvellement
    assert env.auth.access_token_for(SUB) == "AT1"
    env.coeur.auth.refresh.assert_not_called()


def test_sans_refresh_token_le_jeton_d_acces_est_le_secret(env):
    env.coeur.auth.exchange_code.return_value = _grant(refresh="")
    assert "connected" in _callback({"code": "c", "state": _etat(env)}).headers["location"]
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "AT1" and ligne["meta"]["token_shape"] == "access"
    assert "refresh_expires_at" not in ligne["meta"]
    env.auth._JETONS.clear()  # un autre process : il relit la ligne
    assert env.auth.access_token_for(SUB) == "AT1"
    env.coeur.auth.refresh.assert_not_called()


def test_l_identite_est_facultative(env):
    env.coeur.LinkedInAdsClient.return_value.list_account_roles.side_effect = RuntimeError(
        "403")
    assert "connected" in _callback({"code": "c", "state": _etat(env)}).headers["location"]
    assert "member_urn" not in env.coffre.lignes[("member", MEMBRE, "")]["meta"]


def test_le_retour_ne_journalise_ni_le_code_ni_le_jeton(env, caplog):
    env.coeur.auth.exchange_code.side_effect = RuntimeError("refus")
    with caplog.at_level("DEBUG"):
        resp = _callback({"code": "code-tres-secret", "state": _etat(env)})
    assert "error" in resp.headers["location"]
    assert "code-tres-secret" not in caplog.text
    assert not env.coffre.lignes


def test_un_state_rejoue_est_refuse(env):
    etat = _etat(env)
    assert "connected" in _callback({"code": "c1", "state": etat}).headers["location"]
    assert "error" in _callback({"code": "c2", "state": etat}).headers["location"]
    assert env.coeur.auth.exchange_code.call_count == 1


def test_droits_retires_entre_le_depart_et_le_retour(env):
    etat = _etat(env)
    env.membres.clear()
    assert "forbidden" in _callback({"code": "c", "state": etat}).headers["location"]
    etat = _etat(env)
    env.membres.add((SUB, ORG))
    env.coupes.add((CONNECTEUR, ORG))
    assert "forbidden" in _callback({"code": "c", "state": etat}).headers["location"]
    env.coeur.auth.exchange_code.assert_not_called()


@pytest.mark.parametrize("erreur", ["user_cancelled_authorize", "user_cancelled_login",
                                    "access_denied"])
def test_les_refus_de_linkedin_se_lisent_comme_un_refus(env, erreur):
    resp = _callback({"error": erreur, "state": _etat(env)})
    assert "connect=denied" in resp.headers["location"]
    env.coeur.auth.exchange_code.assert_not_called()


def test_une_autre_erreur_reste_une_erreur(env):
    resp = _callback({"error": "invalid_scope", "state": _etat(env)})
    assert "connect=error" in resp.headers["location"]


def test_la_query_du_retour_est_masquee_au_journal_d_acces():
    from oto_mcp import journal_secrets

    ligne = journal_secrets.chemin_pour_journal_acces(
        "/api/linkedin_ads/oauth/callback?code=c-secret&state=s-secret")
    assert "c-secret" not in ligne and "s-secret" not in ligne


# ── Renouvellement ──────────────────────────────────────────────────────────

_META = {"token_shape": "refresh", "member_urn": "urn:li:person:abc"}


def test_renouvelle_et_garde_en_cache(env):
    env.coffre.poser("RT1", _META)
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.coeur.auth.refresh.call_args.args == (
        "id-de-l-application", "secret-de-l-application", "RT1")
    assert env.coffre.lignes[("member", MEMBRE, "")]["secret"] == "RT1"
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.coeur.auth.refresh.call_count == 1


def test_une_rotation_eventuelle_est_ecrite_sous_le_verrou(env):
    env.coffre.poser("RT1", _META)
    env.coeur.auth.refresh.return_value = _grant("AT2", "RT2")
    assert env.auth.access_token_for(SUB) == "AT2"
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT2" and ligne["conn"] is env.conn
    assert ligne["meta"]["member_urn"] == "urn:li:person:abc"


def test_renouvellement_concurrent_reprend_le_jeton_neuf(env):
    env.coffre.poser("RT1", _META)
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.auth._renouveler(("member", MEMBRE, ""), "RT1") == "AT2"
    assert env.coeur.auth.refresh.call_count == 1


def test_un_refus_apres_reconnexion_n_est_pas_une_autorisation_morte(env):
    env.coffre.poser("RT1", _META)

    def refresh(cid, secret, rt):
        if rt == "RT1":
            env.coffre.poser("RT9", _META)  # reconnecté pendant l'appel
            raise env.coeur.auth.LinkedInAdsGrantExpired("invalid")
        return _grant("AT9", "RT9")
    env.coeur.auth.refresh.side_effect = refresh
    assert env.auth.access_token_for(SUB) == "AT9"
    assert not env.coffre.lignes[("member", MEMBRE, "")]["meta"].get("health_ko")


def test_autorisation_morte_marque_la_ligne_sans_la_purger(env):
    env.coffre.poser("RT1", _META)
    env.coeur.auth.refresh.side_effect = env.coeur.auth.LinkedInAdsGrantExpired("expired")
    with pytest.raises(env.auth.LinkedInAdsReauthRequired, match="reconnect"):
        env.auth.access_token_for(SUB)
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT1" and ligne["meta"]["health_ko"] is True
    assert env.auth._etape_manquante(SUB, None, None, {}) == "Sign-in expired — reconnect"


def test_un_jeton_d_acces_seul_expire_marque_la_ligne(env):
    env.coffre.poser("AT-vieux", {"token_shape": "access",
                                  "access_expires_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(env.auth.LinkedInAdsReauthRequired, match="60-day"):
        env.auth.access_token_for(SUB)
    env.coeur.auth.refresh.assert_not_called()
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "AT-vieux" and ligne["meta"]["health_ko"] is True


def test_application_refusee_ne_marque_pas_la_personne(env):
    env.coffre.poser("RT1", _META)
    env.coeur.auth.refresh.side_effect = env.coeur.auth.LinkedInAdsAuthError(
        "invalid_client", status_code=401, code="invalid_client")
    with pytest.raises(env.auth.LinkedInAdsRenewalRefused, match="invalid_client"):
        env.auth.access_token_for(SUB)
    assert not env.coffre.lignes[("member", MEMBRE, "")]["meta"].get("health_ko")


def test_non_connecte_dit_quoi_faire(env):
    assert env.auth._etape_manquante(SUB, None, None, {}) == "Sign in with LinkedIn"


# ── Outil ───────────────────────────────────────────────────────────────────

@pytest.fixture
def outil(env, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp.tools import linkedin_ads as li_tools

    monkeypatch.setattr(li_tools, "_GARDE", li_tools._Garde())
    env.coffre.poser("RT1", _META)
    m = FastMCP("t")
    li_tools.register(m)
    t = asyncio.run(m.get_tool("linkedin_ads"))
    return types.SimpleNamespace(tool=t, fn=t.fn, mod=li_tools,
                                 client=env.coeur.LinkedInAdsClient.return_value)


def test_un_seul_outil_en_lecture_sans_op_a_effet(outil):
    from oto_mcp.tools.lecture import LECTURE

    assert outil.tool.annotations == LECTURE
    assert outil.tool.description, "la docstring doit être servie (pas de f-string)"
    for op in outil.mod._OPS:
        assert not op.startswith(("create", "update", "delete", "set", "add", "pause",
                                  "remove", "post", "send")), op


def test_op_appelle_la_methode_attendue(outil):
    outil.client.search_campaigns.return_value = {"elements": [], "next_page_token": None}
    outil.fn(op="list_campaigns", account_id=_COMPTE, statuses=["ACTIVE"],
             campaign_types=["SPONSORED_UPDATES"], page_size=50)
    args, kw = outil.client.search_campaigns.call_args
    assert args == (_COMPTE,)
    assert kw == {"statuses": ["ACTIVE"], "campaign_group_ids": None,
                  "types": ["SPONSORED_UPDATES"], "test": None, "page_size": 50,
                  "page_token": None}
    outil.fn(op="get_creative", account_id=_COMPTE, creative_id="9")
    assert outil.client.get_creative.call_args.args == (_COMPTE, "9")
    outil.fn(op="list_creatives", account_id=_COMPTE, statuses=["ACTIVE"])
    assert outil.client.search_creatives.call_args.kwargs["intended_statuses"] == ["ACTIVE"]
    outil.fn()
    outil.client.list_account_roles.assert_called_once_with()


def test_argument_que_l_op_ne_lit_pas_est_refuse(outil):
    with pytest.raises(McpError, match="does not take `pivots`"):
        outil.fn(op="list_campaigns", account_id=_COMPTE, pivots=["CAMPAIGN"])
    with pytest.raises(McpError, match="does not take `full`"):
        outil.fn(op="list_creatives", account_id=_COMPTE, full=True)
    outil.client.search_campaigns.assert_not_called()


def test_argument_requis_nomme(outil):
    with pytest.raises(McpError, match="requires `account_id`.*account_roles"):
        outil.fn(op="list_campaigns")
    with pytest.raises(McpError, match="requires `pivots`, `start`"):
        outil.fn(op="analytics", account_ids=[_COMPTE])


def test_la_campagne_resserree_garde_budget_enchere_et_resume_du_ciblage(outil):
    campagne = {
        "id": 1, "name": "C", "status": "ACTIVE", "dailyBudget": {"amount": "50"},
        "unitCost": {"amount": "8"}, "costType": "CPC", "changeAuditStamps": {"x": 1},
        "targetingCriteria": {
            "include": {"and": [
                {"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:105015875"]}},
                {"or": {"urn:li:adTargetingFacet:titles": [f"urn:li:title:{i}"
                                                           for i in range(12)]}}]},
            "exclude": {"or": {"urn:li:adTargetingFacet:seniorities":
                               ["urn:li:seniority:1"]}}}}
    outil.client.get_campaign.return_value = campagne
    out = outil.fn(op="get_campaign", account_id=_COMPTE, campaign_id="1")
    assert "changeAuditStamps" not in out and "targetingCriteria" not in out
    assert out["dailyBudget"] == {"amount": "50"} and out["costType"] == "CPC"
    resume = out["targeting_summary"]
    assert resume["include"]["locations"] == {"count": 1, "values": ["urn:li:geo:105015875"]}
    assert resume["include"]["titles"]["count"] == 12
    assert len(resume["include"]["titles"]["values"]) == 10
    assert resume["exclude"]["seniorities"]["count"] == 1
    assert outil.fn(op="get_campaign", account_id=_COMPTE, campaign_id="1",
                    full=True) is campagne


def test_un_rapport_vide_dit_qu_il_peut_s_agir_d_un_acces_manquant(outil):
    outil.client.get_analytics.return_value = {"elements": [], "paging": {}}
    out = outil.fn(op="analytics", pivots=["CAMPAIGN"], start="2026-10-01",
                   account_ids=[_COMPTE])
    assert "no read access" in out["hint"]
    args, kw = outil.client.get_analytics.call_args
    assert args == (["CAMPAIGN"], "2026-10-01", None)
    assert kw["time_granularity"] == "ALL" and kw["account_ids"] == [_COMPTE]


def test_des_lignes_quotidiennes_sur_plus_de_92_jours_sont_refusees(outil):
    with pytest.raises(McpError, match="MONTHLY"):
        outil.fn(op="analytics", pivots=["CAMPAIGN"], start="2026-01-01",
                 end="2026-06-30", time_granularity="DAILY", account_ids=[_COMPTE])
    outil.client.get_analytics.assert_not_called()


def test_la_garde_par_personne_refuse_avant_d_appeler(outil, monkeypatch):
    outil.client.get_analytics.return_value = {"elements": [{"impressions": 1}]}
    monkeypatch.setitem(outil.mod._Garde.FENETRES, "analytics", (60.0, 2))
    for _ in range(2):
        outil.fn(op="analytics", pivots=["CAMPAIGN"], start="2026-10-01",
                 account_ids=[_COMPTE])
    with pytest.raises(McpError, match="Guard: at most 2 analytics.*Wait"):
        outil.fn(op="analytics", pivots=["CAMPAIGN"], start="2026-10-01",
                 account_ids=[_COMPTE])
    assert outil.client.get_analytics.call_count == 2
    # les autres lectures restent servies
    outil.fn(op="account_roles")


def test_la_garde_compte_les_valeurs_de_metriques_rendues(outil, monkeypatch):
    monkeypatch.setattr(outil.mod._Garde, "VALEURS_MAX", 100)
    outil.client.get_analytics.return_value = {"elements": [{"impressions": 1}] * 20}
    outil.fn(op="analytics", pivots=["CREATIVE"], start="2026-10-01",
             account_ids=[_COMPTE], fields=["impressions", "clicks", "costInUsd",
                                            "likes", "shares"])
    with pytest.raises(McpError, match="too many metric values"):
        outil.fn(op="analytics", pivots=["CREATIVE"], start="2026-10-01",
                 account_ids=[_COMPTE])


def test_la_garde_est_par_personne(outil, monkeypatch):
    g = outil.mod._Garde()
    monkeypatch.setitem(outil.mod._Garde.FENETRES, "call", (60.0, 1))
    g.entrer("a", False)
    g.entrer("b", False)
    with pytest.raises(McpError):
        g.entrer("a", False)


def test_refus_d_argument_du_client_lisible(outil):
    outil.client.get_campaign.side_effect = ValueError("`campaign_id` must be digits.")
    with pytest.raises(McpError, match="must be digits.*Nothing was done"):
        outil.fn(op="get_campaign", account_id=_COMPTE, campaign_id="x")


def test_un_401_oublie_le_jeton_et_reessaie_une_fois(env, outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.list_account_roles.side_effect = [UpstreamHTTPError(401, "expired"),
                                                   {"elements": []}]
    env.coeur.auth.refresh.side_effect = [_grant("AT2"), _grant("AT3")]
    assert outil.fn(op="account_roles") == {"elements": []}
    assert env.coeur.auth.refresh.call_count == 2


def test_deux_401_marquent_la_ligne(env, outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.list_account_roles.side_effect = UpstreamHTTPError(401, "revoked")
    env.coeur.auth.refresh.side_effect = [_grant("AT2"), _grant("AT3")]
    with pytest.raises(McpError, match="reconnect"):
        outil.fn(op="account_roles")
    assert env.coffre.lignes[("member", MEMBRE, "")]["meta"]["health_ko"] is True


def test_un_429_n_est_pas_rejoue_et_dit_d_attendre(outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.list_account_roles.side_effect = UpstreamHTTPError(
        429, {"message": "Too Many Requests"}, retry_after=120)
    with pytest.raises(McpError, match=r"\(429\).*wait 120 s.*Do not retry"):
        outil.fn(op="account_roles")
    assert outil.client.list_account_roles.call_count == 1


def test_un_403_dit_qu_il_faut_un_role_sur_le_compte(outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.get_ad_account.side_effect = UpstreamHTTPError(403, "ACCESS_DENIED")
    with pytest.raises(McpError) as e:
        outil.fn(op="get_account", account_id=_COMPTE)
    msg = e.value.error.message
    assert "VIEWER" in msg
    assert "<upstream-error-body>\nACCESS_DENIED\n</upstream-error-body>" in msg


def test_5xx_reste_ce_qu_il_est(outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.list_account_roles.side_effect = UpstreamHTTPError(503, "down")
    with pytest.raises(UpstreamHTTPError):
        outil.fn(op="account_roles")


def test_autorisation_morte_devient_un_refus_nomme(env, outil):
    env.coeur.auth.refresh.side_effect = env.coeur.auth.LinkedInAdsGrantExpired("expired")
    with pytest.raises(McpError, match="reconnect"):
        outil.fn(op="account_roles")
    outil.client.list_account_roles.assert_not_called()


@pytest.mark.exige_pin_oto_core
def test_les_arguments_passes_existent_sur_les_methodes_de_la_lib():
    """Chaque argument nommé que l'outil passe existe sur la vraie méthode de la lib
    épinglée : un renommage côté lib rougit ici, pas en prod."""
    from oto.tools.linkedin_ads import LinkedInAdsClient

    attendus = {
        "search_ad_accounts": {"statuses", "test", "page_size", "page_token"},
        "search_campaign_groups": {"account_id", "statuses", "test", "page_size",
                                   "page_token"},
        "search_campaigns": {"account_id", "statuses", "campaign_group_ids", "types",
                             "test", "page_size", "page_token"},
        "search_creatives": {"account_id", "campaign_ids", "intended_statuses",
                             "page_size", "page_token"},
        "get_analytics": {"pivots", "start", "end", "time_granularity", "account_ids",
                          "campaign_group_ids", "campaign_ids", "fields"},
    }
    for nom, params in attendus.items():
        sig = set(inspect.signature(getattr(LinkedInAdsClient, nom)).parameters) - {"self"}
        assert params <= sig, (nom, params - sig)

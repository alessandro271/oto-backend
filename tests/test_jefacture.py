"""Le connecteur `jefacture` — connexion OAuth de la personne (code d'autorisation,
application plateforme, refresh token qui tourne) et l'outil de lecture.

⚠️ Le cœur est MOQUÉ à sa frontière (`oto.tools.jefacture` et son `auth` posés dans
`sys.modules`), le coffre et les réglages sont des faux en mémoire : aucun appel réel
à l'API ni à la base. La RÉSOLUTION du credential, elle, est la vraie
(`access.resolve_credential`). Vérifié : le registre, l'application absente nommée,
le flux hébergé (state signé à usage unique), le retour de connexion (identité en
meta), le renouvellement (cache, rotation écrite sous le verrou, concurrence,
autorisation morte MARQUÉE et jamais purgée, application refusée sans marque), et
côté outil la correspondance op → méthode, le refus d'un argument que l'op ne lit
pas, l'argument requis nommé, le refus d'argument du client, le 401 rejoué une fois.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

CONNECTEUR = "jefacture"
ORG = 42
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
_RETOUR = "https://mcp.exemple.test/api/jefacture/oauth/callback"
_ESPACE = "4b0a3c5e-0d1f-4c2a-9f6e-1a2b3c4d5e6f"
_DOC = "1d0159ca-ab19-49d5-9e13-9d34afb03849"
_APP = {"client_id": "id-de-l-application", "client_secret": "secret-de-l-application"}


def _grant(access="AT1", refresh="RT1", expires_in=3600):
    return types.SimpleNamespace(access_token=access, refresh_token=refresh,
                                 expires_in=expires_in, scope="openid")


def _faux_coeur():
    mod = types.ModuleType("oto.tools.jefacture")
    auth = types.ModuleType("oto.tools.jefacture.auth")

    class JefactureAuthError(ValueError):
        def __init__(self, message, *, status_code=401, code=None):
            super().__init__(message)
            self.status_code, self.code = status_code, code

    class JefactureGrantExpired(JefactureAuthError):
        pass

    auth.JefactureAuthError = JefactureAuthError
    auth.JefactureGrantExpired = JefactureGrantExpired
    auth.authorize_url = MagicMock(return_value="https://auth.exemple.test/oauth2/auth?x=1")
    auth.exchange_code = MagicMock(return_value=_grant())
    auth.refresh = MagicMock(return_value=_grant("AT2", "RT2"))
    mod.auth = auth
    mod.JefactureClient = MagicMock(name="JefactureClient")
    mod.JefactureClient.return_value.get_me.return_value = {
        "email": "personne@example.test", "firstName": "P"}
    return mod


class _Coffre:
    def __init__(self):
        self.lignes: dict[tuple, dict] = {}

    def poser(self, secret, meta=None):
        self.lignes[("member", MEMBRE, "")] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": SUB,
            "set_at": "2026-10-22T00:00:00Z"}

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
            "set_at": "2026-10-22T01:00:00Z", "conn": conn}

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
    from oto_mcp.auth import jefacture as jf_auth
    from oto_mcp.connectors import activation, cardinality
    from oto_mcp.db import connector_settings as store

    mod = _faux_coeur()
    monkeypatch.setitem(sys.modules, "oto.tools.jefacture", mod)
    monkeypatch.setitem(sys.modules, "oto.tools.jefacture.auth", mod.auth)
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
    monkeypatch.setattr(jf_auth, "_JETONS", {})
    # Le verrou de ligne est un verrou consultatif PG : ici, un verrou en mémoire qui
    # VOIT les prises et rend une connexion repérable.
    verrou = threading.Lock()
    prises: list = []
    conn = object()

    @contextlib.contextmanager
    def _verrou_ligne(ligne):
        with verrou:
            prises.append(ligne)
            yield conn
    monkeypatch.setattr(jf_auth, "_verrou_ligne", _verrou_ligne)
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
    return types.SimpleNamespace(coeur=mod, coffre=coffre, auth=jf_auth, reglages=reglages,
                                 prises=prises, conn=conn, membres=membres, coupes=coupes)


# ── Registre ────────────────────────────────────────────────────────────────

def test_registre():
    c = providers.REGISTRY[CONNECTEUR]
    assert c.secret_kind == "oauth" and c.auth_modes == frozenset({"byo_user"})
    assert c.personal_session
    assert c.publisher_name
    assert not c.credential_fields
    assert c.doc_sections, "la fiche doit être servie depuis son markdown"


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
        "jefacture.com app to be configured by the operator")
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
        "ubersuggest", {"sub": SUB, "org": ORG, "jti": "j"})) is None
    assert env.auth.verify_state(oauth_flow.sign_state(
        "jefacture", {"sub": SUB, "org": ORG})) is None  # sans jti : refusé


def test_le_dialogue_porte_le_client_de_l_application_et_le_retour(env):
    env.auth.build_auth_url(SUB, "")
    client_id, retour, etat = env.coeur.auth.authorize_url.call_args.args
    assert client_id == "id-de-l-application" and retour == _RETOUR
    assert env.auth.verify_state(etat)["sub"] == SUB


# ── Retour de connexion ─────────────────────────────────────────────────────

def _callback(query: dict):
    from starlette.requests import Request

    from oto_mcp.api import jefacture as api_jf

    route = api_jf.make_routes(None, None, None, None, None)[0]
    qs = "&".join(f"{k}={v}" for k, v in query.items()).encode()
    req = Request({"type": "http", "method": "GET", "path": route.path,
                   "query_string": qs, "headers": []})
    return asyncio.run(route.endpoint(req))


def test_retour_echange_puis_range_le_refresh_token_et_l_identite(env):
    resp = _callback({"code": "le-code", "state": _etat(env)})
    assert resp.status_code == 302 and "connected" in resp.headers["location"]
    assert env.coeur.auth.exchange_code.call_args.args == (
        "id-de-l-application", "secret-de-l-application", "le-code", _RETOUR)
    env.coeur.JefactureClient.assert_called_with(access_token="AT1")
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT1"
    assert ligne["meta"]["email"] == "personne@example.test"
    assert ligne["meta"]["connected_at"]
    assert ligne["conn"] is env.conn, "la pose se fait sous le verrou de la ligne"
    # le jeton d'accès de l'échange sert tout de suite, sans renouvellement
    assert env.auth.access_token_for(SUB) == "AT1"
    env.coeur.auth.refresh.assert_not_called()


def test_retour_sans_identite_ne_range_rien(env):
    env.coeur.JefactureClient.return_value.get_me.return_value = {"firstName": "P"}
    resp = _callback({"code": "c", "state": _etat(env)})
    assert "error" in resp.headers["location"]
    assert not env.coffre.lignes


def test_le_retour_ne_journalise_ni_le_code_ni_le_jeton(env, caplog):
    env.coeur.auth.exchange_code.side_effect = RuntimeError("refus")
    with caplog.at_level("DEBUG"):
        resp = _callback({"code": "code-tres-secret", "state": _etat(env)})
    assert "error" in resp.headers["location"]
    assert "code-tres-secret" not in caplog.text


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


def test_retour_refuse_par_la_personne(env):
    resp = _callback({"error": "access_denied", "state": _etat(env)})
    assert "denied" in resp.headers["location"]
    env.coeur.auth.exchange_code.assert_not_called()


def test_retour_sans_state_lisible(env):
    resp = _callback({"code": "x", "state": "faux.state"})
    assert "error" in resp.headers["location"]
    env.coeur.auth.exchange_code.assert_not_called()


def test_la_query_du_retour_est_masquee_au_journal_d_acces():
    from oto_mcp import journal_secrets

    ligne = journal_secrets.chemin_pour_journal_acces(
        "/api/jefacture/oauth/callback?code=c-secret&state=s-secret")
    assert "c-secret" not in ligne and "s-secret" not in ligne


# ── Renouvellement ──────────────────────────────────────────────────────────

_META = {"email": "personne@example.test"}


def test_renouvelle_garde_en_cache_et_ecrit_la_rotation_sous_le_verrou(env):
    env.coffre.poser("RT1", _META)
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.coeur.auth.refresh.call_args.args == (
        "id-de-l-application", "secret-de-l-application", "RT1")
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT2" and ligne["conn"] is env.conn
    assert ligne["meta"]["email"] == "personne@example.test"
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.coeur.auth.refresh.call_count == 1


def test_renouvellement_concurrent_reprend_le_jeton_neuf_sans_rejouer(env):
    """Le second appelant a résolu RT1 mais attendait le verrou : il relit la ligne
    (RT2), trouve le jeton neuf, ne rejoue pas RT1 — déjà consommé chez l'éditeur."""
    env.coffre.poser("RT1", _META)
    assert env.auth.access_token_for(SUB) == "AT2"
    assert env.auth._renouveler(("member", MEMBRE, ""), "RT1") == "AT2"
    assert env.coeur.auth.refresh.call_count == 1


def test_un_refus_apres_reconnexion_n_est_pas_une_autorisation_morte(env):
    env.coffre.poser("RT1", _META)

    def refresh(cid, secret, rt):
        if rt == "RT1":
            env.coffre.poser("RT9", _META)  # reconnecté pendant l'appel
            raise env.coeur.auth.JefactureGrantExpired("invalid_grant")
        return _grant("AT9", "RT9")
    env.coeur.auth.refresh.side_effect = refresh
    assert env.auth.access_token_for(SUB) == "AT9"
    assert not env.coffre.lignes[("member", MEMBRE, "")]["meta"].get("health_ko")


def test_autorisation_morte_marque_la_ligne_sans_la_purger(env):
    env.coffre.poser("RT1", _META)
    env.coeur.auth.refresh.side_effect = env.coeur.auth.JefactureGrantExpired("invalid_grant")
    with pytest.raises(env.auth.JefactureReauthRequired, match="reconnect"):
        env.auth.access_token_for(SUB)
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "RT1" and ligne["meta"]["health_ko"] is True
    assert env.auth._etape_manquante(SUB, None, None, {}) == "Sign-in expired — reconnect"


def test_application_refusee_ne_marque_pas_la_personne(env):
    env.coffre.poser("RT1", _META)
    env.coeur.auth.refresh.side_effect = env.coeur.auth.JefactureAuthError(
        "invalid_client", status_code=401, code="invalid_client")
    with pytest.raises(env.auth.JefactureRenewalRefused, match="invalid_client"):
        env.auth.access_token_for(SUB)
    assert not env.coffre.lignes[("member", MEMBRE, "")]["meta"].get("health_ko")


def test_non_connecte_dit_quoi_faire(env):
    assert env.auth._etape_manquante(SUB, None, None, {}) == "Sign in with jefacture.com"


# ── Outil ───────────────────────────────────────────────────────────────────

@pytest.fixture
def outil(env):
    from fastmcp import FastMCP

    from oto_mcp.tools import jefacture as jf_tools

    env.coffre.poser("RT1", _META)
    m = FastMCP("t")
    jf_tools.register(m)
    t = asyncio.run(m.get_tool("jefacture"))
    return types.SimpleNamespace(tool=t, fn=t.fn,
                                 client=env.coeur.JefactureClient.return_value)


def test_un_seul_outil_en_lecture(outil):
    from oto_mcp.tools.lecture import LECTURE

    assert outil.tool.annotations == LECTURE


def test_op_appelle_la_methode_du_meme_nom(outil):
    outil.client.list_documents.return_value = {"documents": []}
    out = outil.fn(op="list_documents", space_id=_ESPACE, folder="PURCHASES",
                   status_query="pay=UNPAID OR pay=PARTIALLY_PAID", page=0)
    assert out == {"documents": []}
    args, kw = outil.client.list_documents.call_args
    assert args == (_ESPACE,)
    assert kw["folder"] == "PURCHASES" and kw["page"] == 0
    assert kw["status_query"] == "pay=UNPAID OR pay=PARTIALLY_PAID"
    outil.fn(op="get_document", space_id=_ESPACE, document_id=_DOC)
    assert outil.client.get_document.call_args.args == (_ESPACE, _DOC)
    outil.fn()
    assert outil.client.list_spaces.call_args.args == (None, None)


def test_argument_que_l_op_ne_lit_pas_est_refuse(outil):
    with pytest.raises(McpError, match="does not take `folder`"):
        outil.fn(op="list_connections", space_id=_ESPACE, folder="SALES")
    with pytest.raises(McpError, match="does not take `space_id`"):
        outil.fn(op="get_me", space_id=_ESPACE)
    outil.client.list_connections.assert_not_called()
    outil.client.get_me.assert_not_called()


def test_argument_requis_nomme(outil):
    with pytest.raises(McpError, match="requires `space_id`.*list_spaces"):
        outil.fn(op="list_documents")
    with pytest.raises(McpError, match="requires `document_id`"):
        outil.fn(op="get_document", space_id=_ESPACE)


def test_refus_d_argument_du_client_lisible(outil):
    outil.client.get_document.side_effect = ValueError(
        "`document_id` must be a UUID; got 'x'.")
    with pytest.raises(McpError, match="must be a UUID.*Nothing was done"):
        outil.fn(op="get_document", space_id=_ESPACE, document_id="x")


def test_un_401_oublie_le_jeton_et_reessaie_une_fois(env, outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.get_me.side_effect = [UpstreamHTTPError(401, "expired"), {"email": "e"}]
    env.coeur.auth.refresh.side_effect = [_grant("AT2", "RT2"), _grant("AT3", "RT3")]
    assert outil.fn(op="get_me") == {"email": "e"}
    assert env.coeur.auth.refresh.call_count == 2


def test_refus_amont_nomme_et_clos(outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.list_payment_accounts.side_effect = UpstreamHTTPError(403, "no product")
    with pytest.raises(McpError) as e:
        outil.fn(op="list_payment_accounts", space_id=_ESPACE)
    msg = e.value.error.message
    assert "(403)" in msg
    assert "<upstream-error-body>\nno product\n</upstream-error-body>" in msg


def test_5xx_reste_ce_qu_il_est(outil):
    from oto.tools.common.errors import UpstreamHTTPError

    outil.client.get_me.side_effect = UpstreamHTTPError(503, "down")
    with pytest.raises(UpstreamHTTPError):
        outil.fn(op="get_me")


def test_autorisation_morte_devient_un_refus_nomme(env, outil):
    env.coeur.auth.refresh.side_effect = env.coeur.auth.JefactureGrantExpired("invalid_grant")
    with pytest.raises(McpError, match="reconnect"):
        outil.fn(op="get_me")
    outil.client.get_me.assert_not_called()


def test_les_arguments_de_chaque_op_sont_ceux_de_la_methode():
    """`_ARGS` (ce que l'outil accepte par op) = la signature de la vraie méthode de la
    lib : un argument accepté que la méthode ne prend pas, ou l'inverse, rougit ici."""
    from oto.tools.jefacture import JefactureClient

    from oto_mcp.tools import jefacture as jf_tools

    for op, (requis, facultatifs) in jf_tools._ARGS.items():
        params = inspect.signature(getattr(JefactureClient, op)).parameters
        noms = [n for n in params if n != "self"]
        assert set(noms) == set(requis) | set(facultatifs), op
        sans_defaut = {n for n, p in params.items()
                       if n != "self" and p.default is inspect.Parameter.empty}
        assert sans_defaut == set(requis), op


def test_les_listes_resserrent_par_defaut_et_full_rend_le_brut(outil):
    """Une liste rend par défaut l'index (numéro, dates, statuts, montants, tiers) et
    laisse le volumineux qu'une autre op sert ; `full=True` rend la page telle quelle,
    l'enveloppe est intacte dans les deux cas (`tests/test_sorties_listes_projetees.py`)."""
    doc = {"id": _DOC, "folder": "SALES", "category": "INVOICE",
           "status": {"document": "VALIDATED", "pay": "UNPAID"},
           "metadata": {"documentNumber": "F-12", "issueDate": "2026-10-01",
                        "customer": {"name": "Client"},
                        "documentTotals": {"payableAmount": 120, "openBalance": 120}},
           "connections": [{"id": "c", "addresses": [{"street": "…"}]}],
           "balanceOffsets": [{"paymentId": "p"}]}
    page = {"documents": [doc], "totalElements": 1, "currentPage": 0, "pageSize": 20}
    outil.client.list_documents.return_value = page
    out = outil.fn(op="list_documents", space_id=_ESPACE)
    assert out == {**page, "documents": [{k: v for k, v in doc.items()
                                          if k not in ("connections", "balanceOffsets")}]}
    assert outil.fn(op="list_documents", space_id=_ESPACE, full=True) == page

    outil.client.list_connections.return_value = {"connections": [
        {"id": "c", "displayName": "Fournisseur", "globalIdentifiers": ["FR:SIREN:1"],
         "addresses": [{"street": "…"}], "metadata": {"createdAt": "…"}}]}
    assert outil.fn(op="list_connections", space_id=_ESPACE) == {"connections": [
        {"id": "c", "displayName": "Fournisseur", "globalIdentifiers": ["FR:SIREN:1"]}]}


def test_full_refuse_sur_une_op_qui_rend_entier(outil):
    with pytest.raises(McpError, match="does not take `full`"):
        outil.fn(op="get_document", space_id=_ESPACE, document_id=_DOC, full=True)
    outil.client.get_document.assert_not_called()

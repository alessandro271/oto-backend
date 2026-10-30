"""OAuth Zoho server-based — le SECOND mode d'acquisition (le Self Client reste).

L'invariant qui rend la cohabitation sûre : **les deux modes produisent le même
credential** (client_id + client_secret + refresh_token + data_center). Le client
oto-core, la résolution et les outils ne connaissent pas le mode d'acquisition.
"""
from __future__ import annotations

import time

import pytest

from oto_mcp.auth import zoho as z

DC = "eu"
APP = {"client_id": "1000.ORGAPP", "client_secret": "org-secret"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-state-secret")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")


# --- l'app vient du COFFRE (cascade), jamais de l'environnement --------------

def test_app_comes_from_the_resolved_credential():
    """client_id/secret sont posés sur la carte comme n'importe quel credential —
    membre, équipe, org ou plateforme : la cascade tranche, pas une variable d'env."""
    assert z.resolve_app(APP) == ("1000.ORGAPP", "org-secret")


def test_no_app_yet_is_actionable():
    with pytest.raises(z.ZohoOAuthError, match="client ID"):
        z.resolve_app({})


class _Resolved:
    def __init__(self, fields, mode="user"):
        self.fields, self.mode = fields, mode


def test_app_fields_reads_the_cascade(monkeypatch):
    seen = {}
    monkeypatch.setattr(z.access, "resolve_credential",
                        lambda con, want=None, sub=None, emit_on_failure=True, **kw:
                        seen.update(con=con, want=want, sub=sub,
                                    emit=emit_on_failure) or _Resolved(APP))
    assert z.app_fields("zohodesk", "u1", org=None, group=None) == APP
    # sub EXPLICITE (route REST, hors contexte MCP) + sonde silencieuse
    assert seen == {"con": "zohodesk", "want": "byo", "sub": "u1", "emit": False}


def test_app_fields_is_empty_before_any_credential(monkeypatch):
    """Première connexion : rien de posé — état NOMINAL, pas une erreur.

    ⚠️ Le stub lève la vraie exception de la cascade, une **`McpError`** : c'est elle,
    et elle seule, qui dit « pas encore de credential ». Il levait un `ValueError`
    jusqu'au 2026-08-27 — une exception que `resolve_credential` ne produit jamais.
    Tant qu'`app_fields` attrapait `Exception`, l'écart ne se voyait pas ; il cachait
    précisément le défaut B7 de l'inventaire des silences (une erreur de COFFRE se
    lisait comme « pas de credential » et faisait basculer sur l'app d'éditeur oto).
    Même leçon que `test_we_call_an_api_that_actually_accepts_sub` ci-dessous : un
    stub qui ment sur la forme du réel masque le bug qu'on croit tester."""
    from oto_mcp.mcp_errors import McpError
    from mcp.types import ErrorData, INVALID_PARAMS

    def _aucun_credential(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Aucune clé zoho posée"))

    monkeypatch.setattr(z.access, "resolve_credential", _aucun_credential)
    assert z.app_fields("zoho", "u1", org=None, group=None) == {}
    assert z.has_app("zoho", "u1", org=None, group=None) is False


def test_we_call_an_api_that_actually_accepts_sub():
    """GARDE-FOU. Un stub de test accepte n'importe quelle signature — il a masqué
    le vrai bug (28/07) : `resolve_credential_fields` n'a PAS de paramètre `sub`,
    l'appel levait un TypeError avalé par le except, et la connexion server-based
    était muette. On vérifie donc la signature RÉELLE de la fonction appelée."""
    import inspect
    params = inspect.signature(z.access.resolve_credential).parameters
    assert "sub" in params, "resolve_credential doit accepter un sub explicite (route REST)"
    assert "emit_on_failure" in params
    # et la fonction SANS sub reste bien inadaptée à notre usage
    assert "sub" not in inspect.signature(z.access.resolve_credential_fields).parameters


# --- state signé -------------------------------------------------------------

def test_state_roundtrip():
    st = z.make_state("u1", 35, "zohodesk", DC, group_id=7, level="read")
    assert z.verify_state(st) == {"sub": "u1", "org": 35, "group": 7,
                                  "connector": "zohodesk", "data_center": DC,
                                  "level": "read", "return_app": ""}


def test_state_carries_the_front_that_asked():
    """Le callback est appelé par Zoho, sans session : le state est la SEULE mémoire
    du demandeur qui survit à l'aller-retour."""
    st = z.make_state("u1", 35, "zoho", DC, "acme", group_id=None, level="full")
    assert z.verify_state(st)["return_app"] == "acme"


def test_state_signed_before_the_field_still_verifies():
    """Tolérance aux states émis AVANT `return_app` et encore vivants dans la fenêtre
    du TTL : on dégrade vers le défaut, on ne casse pas un consentement en cours."""
    from oto_mcp.auth import flow as oauth_flow
    st = oauth_flow.sign_state(z._AUDIENCE, {"sub": "u1", "org": 35, "g": None,
                                             "c": "zoho", "dc": DC, "l": "full"})
    assert z.verify_state(st)["return_app"] == ""


@pytest.mark.parametrize("manque", ["g", "l"])
def test_state_without_team_or_level_is_refused(manque):
    """Sans l'équipe ni le niveau, le retour ne peut pas rejouer la résolution de l'aller
    (l'app d'une équipe, les scopes demandés) : refusé, on ne devine pas."""
    from oto_mcp.auth import flow as oauth_flow
    charge = {"sub": "u1", "org": 35, "g": 3, "c": "zoho", "dc": DC, "l": "read"}
    del charge[manque]
    assert z.verify_state(oauth_flow.sign_state(z._AUDIENCE, charge)) is None


@pytest.mark.parametrize("connector, level", [("zoho", "admin"), ("zohoanalytics", "read")])
def test_state_with_an_undeclared_level_is_refused(connector, level):
    st = z.make_state("u1", 35, connector, DC, group_id=None, level=level)
    assert z.verify_state(st) is None


def test_forged_state_is_rejected():
    st = z.make_state("u1", 35, "zohodesk", DC, group_id=None, level="full")
    payload, _sig = st.split(".", 1)
    assert z.verify_state(f"{payload}.{'A' * 43}") is None


def test_state_from_another_secret_is_rejected(monkeypatch):
    st = z.make_state("u1", 35, "zohodesk", DC, group_id=None, level="full")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "autre-secret")
    assert z.verify_state(st) is None


def test_expired_state_is_rejected(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000_000)
    st = z.make_state("u1", 35, "zohodesk", DC, group_id=None, level="full")
    monkeypatch.setattr(time, "time", lambda: 1_000_000 + z._STATE_TTL + 1)
    assert z.verify_state(st) is None


@pytest.mark.parametrize("bad", ["", "abc", "a.b", None])
def test_malformed_state_is_rejected(bad):
    assert z.verify_state(bad) is None


# --- URL d'autorisation ------------------------------------------------------

def test_auth_url_declares_our_scopes():
    """LE bénéfice du mode : les scopes viennent de NOUS, plus de l'utilisateur —
    trois incidents de scope venaient de là (#190, #202, Desk articles-only)."""
    url = z.build_auth_url("u1", 35, "zohodesk", DC, app=APP, group_id=None, level="full")
    assert "accounts.zoho.eu/oauth/v2/auth" in url
    assert "Desk.search.READ" in url and "Desk.articles.READ" in url


def test_auth_url_asks_for_offline_access():
    """Sans `access_type=offline` + `prompt=consent`, Zoho ne renvoie PAS de
    refresh_token → la connexion mourrait au bout d'une heure."""
    url = z.build_auth_url("u1", 35, "zoho", DC, app=APP, group_id=None, level="full")
    assert "access_type=offline" in url and "prompt=consent" in url


def test_auth_url_uses_the_single_redirect_uri():
    """Une seule URI pour les 3 connecteurs (le connecteur voyage dans le state) :
    une URI doit être enregistrée au byte près côté Zoho."""
    a = z.build_auth_url("u1", 35, "zoho", DC, app=APP, group_id=None, level="full")
    b = z.build_auth_url("u1", 35, "zohodesk", DC, app=APP, group_id=None, level="full")
    assert z.redirect_uri() == "https://mcp.oto.cx/api/zoho/oauth/callback"
    for u in (a, b):
        assert "mcp.oto.cx%2Fapi%2Fzoho%2Foauth%2Fcallback" in u


def test_unknown_region_is_refused():
    with pytest.raises(z.ZohoOAuthError, match="data center"):
        z.build_auth_url("u1", 35, "zoho", "xx", app=APP, group_id=None, level="full")


def test_unknown_connector_is_refused():
    with pytest.raises(z.ZohoOAuthError):
        z.build_auth_url("u1", 35, "salesforce", DC, app=APP, group_id=None, level="full")


# --- échange du code ---------------------------------------------------------

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def test_expired_code_gives_an_actionable_message(monkeypatch):
    monkeypatch.setattr(z.oauth_flow.requests, "post",
                        lambda url, **kw: _Resp(200, {"error": "invalid_code"}))
    with pytest.raises(z.ZohoOAuthError, match="expire"):
        z.exchange_code("vieux", DC, app=APP)


def test_missing_refresh_token_is_explained(monkeypatch):
    """Piège Zoho : une app déjà autorisée ne renvoie plus de refresh_token —
    silencieux et incompréhensible sans ce message."""
    monkeypatch.setattr(z.oauth_flow.requests, "post",
                        lambda url, **kw: _Resp(200, {"access_token": "at"}))
    with pytest.raises(z.ZohoOAuthError, match="Connected Apps"):
        z.exchange_code("code", DC, app=APP)


# --- persistance : MÊME forme que le Self Client -----------------------------

def test_persist_writes_the_same_fields_as_self_client(monkeypatch):
    """L'invariant de cohabitation : après un OAuth, le credential est
    indistinguable de celui posé à la main → client et résolution inchangés."""
    written = {}
    monkeypatch.setattr(z.credentials_store, "set_credential",
                        lambda et, eid, con, secret, **kw: written.update(
                            entity_type=et, entity_id=eid, connector=con,
                            secret=secret, meta=kw.get("meta")))
    z.persist("u1", 35, "zohodesk", DC,
              {"refresh_token": "1000.rt", "scope": "Desk.articles.READ"}, app=APP,
              level="read")
    fields = z.credentials_store.unpack_secret("zohodesk", written["secret"])
    assert fields == {"client_id": "1000.ORGAPP", "client_secret": "org-secret",
                      "refresh_token": "1000.rt", "data_center": "eu"}
    # ⚠️ l'id de membre est (org, sub) — et l'AAD de chiffrement en DÉRIVE : inverser
    # l'ordre écrit un credential indéchiffrable, invisible de la cascade (vécu 28/07).
    from oto_mcp import credentials_store as _cs
    assert written["entity_type"] == "member"
    assert written["entity_id"] == _cs.member_id(35, "u1") == "35:u1"
    assert written["meta"]["acquired_via"] == "oauth"
    # Le niveau demandé et les scopes que Zoho dit avoir accordés restent avec le compte.
    assert written["meta"]["access_level"] == "read"
    assert written["meta"]["scope"] == "Desk.articles.READ"


def test_supports_only_the_three_zoho_connectors():
    assert z.supports("zoho") and z.supports("zohodesk") and z.supports("zohoanalytics")
    assert not z.supports("salesforce")


# --- l'étape manquante remonte au front (seam status_hints) ------------------

def test_pending_action_when_app_posted_but_no_consent(monkeypatch):
    """Connexion en deux temps : app posée, consentement pas donné → le front doit
    pouvoir afficher l'étape suivante. Sans ça la carte paraît configurée et
    échoue au premier appel (et l'encart OAuth disparaîtrait de l'écran)."""
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho as zoho_tools  # enregistre les hooks à l'import
    assert status_hints.has_hook("zohodesk")

    monkeypatch.setattr(zoho_tools.access, "resolve_credential",
                        lambda *a, **k: _Resolved({"client_id": "1000.X",
                                                   "client_secret": "s",
                                                   "data_center": "eu"}))
    action = status_hints.pending_action("zohodesk", "u1", 35, None, {"mode": "user"})
    assert action == "Authorize oto with Zoho"


def test_no_pending_action_once_consent_given(monkeypatch):
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho as zoho_tools
    monkeypatch.setattr(zoho_tools.access, "resolve_credential",
                        lambda *a, **k: _Resolved({"client_id": "1000.X",
                                                   "client_secret": "s",
                                                   "refresh_token": "1000.rt",
                                                   "data_center": "eu"}))
    assert status_hints.pending_action("zohodesk", "u1", 35, None, {"mode": "user"}) is None


def test_no_pending_action_when_nothing_posted():
    """Rien de posé → le verdict « à connecter » suffit, pas de double message."""
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho  # noqa: F401
    assert status_hints.pending_action(
        "zohodesk", "u1", 35, None, {"mode": "forbidden"}) is None


# --- la sonde « tester la connexion » connaît l'état intermédiaire ------------

@pytest.mark.parametrize("module_name", ["zoho", "zohodesk"])
def test_verify_names_the_pending_consent(module_name):
    """Sans garde, la sonde tente un refresh sans refresh_token → Zoho renvoie une
    erreur de grant traduite en « refresh token périmé, régénère-le », qui envoie
    l'utilisateur régénérer ce qui n'existe pas encore (vécu au 1er test réel)."""
    import importlib
    mod = importlib.import_module(f"oto_mcp.tools.{module_name}")
    with pytest.raises(ValueError, match="authorization has not been granted yet"):
        mod._verify({"client_id": "1000.X", "client_secret": "s", "data_center": "eu"})


def test_verify_still_runs_for_a_complete_self_client(monkeypatch):
    """Un credential complet ne doit PAS être arrêté par le garde — il va bien
    jusqu'au vrai test de connexion."""
    from oto_mcp.tools import zoho as zoho_tools
    calls = []
    monkeypatch.setattr(zoho_tools.requests, "post",
                        lambda *a, **k: calls.append(1) or _Resp(200, {"error": "x"}))
    with pytest.raises(ValueError):
        zoho_tools._verify({"client_id": "1000.X", "client_secret": "s",
                            "refresh_token": "1000.rt", "data_center": "eu"})
    assert calls, "la sonde doit avoir tenté le refresh (garde non déclenché)"


# --- A : UN calcul d'état, plusieurs surfaces --------------------------------

def test_state_is_declared_once_for_the_three_connectors():
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho  # noqa: F401 — enregistre à l'import
    app_only = {"client_id": "1000.X", "client_secret": "s", "data_center": "eu"}
    for con in ("zoho", "zohodesk", "zohoanalytics"):
        st = status_hints.credential_state(con, app_only)
        assert st is not None and not st.complete
        assert st.missing == ("refresh_token",)
        assert "Authorize oto with Zoho" in st.next_action


def test_complete_credential_is_complete():
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho  # noqa: F401
    st = status_hints.credential_state("zoho", {"client_id": "1000.X",
                                                "client_secret": "s",
                                                "refresh_token": "1000.rt"})
    assert st is not None and st.complete


def test_probe_and_verdict_share_the_same_text():
    """LE point de A : la sonde et le verdict ne re-dérivent plus chacun leur
    diagnostic — ils rendent le MÊME `next_action`. C'est la divergence entre ces
    deux textes qui a produit « refresh token périmé » face à un consentement
    simplement pas encore donné."""
    import pytest as _pytest
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho as zoho_tools
    app_only = {"client_id": "1000.X", "client_secret": "s", "data_center": "eu"}
    expected = status_hints.credential_state("zoho", app_only).next_action
    with _pytest.raises(ValueError) as e:
        zoho_tools._verify(app_only)
    assert str(e.value) == expected


def test_require_complete_is_a_noop_without_declared_state():
    """Un connecteur qui ne déclare pas d'état n'est pas gêné par le seam."""
    from oto_mcp import status_hints
    status_hints.require_complete("serper", {})   # ne lève pas
    assert status_hints.credential_state("serper", {}) is None


def test_persist_uses_the_canonical_member_id(monkeypatch):
    """GARDE-FOU : ne jamais RECONSTRUIRE un entity_id à la main. `member_id` est le
    seul à connaître l'ordre (org, sub), dont dépend l'AAD de chiffrement — un id
    inversé produit un credential que plus rien ne peut lire."""
    from oto_mcp import credentials_store as cs
    seen = {}
    monkeypatch.setattr(z.credentials_store, "set_credential",
                        lambda et, eid, con, secret, **kw: seen.update(eid=eid))
    z.persist("bn01", 2, "zohodesk", "eu", {"refresh_token": "rt"}, app=APP, level="full")
    assert seen["eid"] == cs.member_id(2, "bn01")
    assert seen["eid"].startswith("2:"), "org d'abord, pas le sub"


def test_verify_capability_returns_pending_not_failure(monkeypatch):
    """La SONDE rend l'état au lieu d'un verdict d'échec quand le credential est
    volontairement incomplet. Sans ce `pending`, le dialogue du dashboard reste
    ouvert sur une correction impossible — « connecter ne fait rien » (vécu 28/07)."""
    from oto_mcp.capabilities.connectors import verify as cv
    from oto_mcp.tools import zoho  # noqa: F401 — déclare l'état
    import asyncio

    # 4e élément = l'INSTANCE sondée (level + ref), ajoutée le 03/08 : un `ok` de sonde
    # doit dire quelle clé a répondu, la cascade pouvant retomber d'un cran.
    monkeypatch.setattr(cv, "_fields_config_scope",
                        lambda ctx, inp: ({"client_id": "1000.X", "client_secret": "s",
                                           "data_center": "eu"}, {}, None,
                                          {"level": "user", "ref": "member:1:sub:zoho"},
                                          ("member", "1:sub", "")))
    monkeypatch.setattr(cv.connector_verify, "supports", lambda p: True)
    def _never(*a, **k):
        raise AssertionError("la sonde ne doit PAS être EXÉCUTÉE sur un credential incomplet")
    monkeypatch.setattr(cv.connector_verify, "probe_for", lambda p: _never,
                        raising=False)

    class _Inp:
        provider, level = "zohodesk", "auto"
    out = asyncio.run(cv._verify(object(), _Inp()))
    assert out["ok"] is False and out["pending"] is True
    assert "Authorize oto with Zoho" in out["error"]


# --- verify-avant-persist : le blocage circulaire ----------------------------

def test_incomplete_credential_skips_the_probe_and_is_persisted():
    """LA cause racine du 28/07. `api_key_save` teste la connexion AVANT d'écrire
    (#106) — sain pour un credential complet, mais fatal pour une connexion en deux
    temps : à l'étape 1 le credential est légitimement incomplet, la sonde échoue par
    construction, la pose est refusée… donc on ne peut jamais consentir, donc jamais
    le compléter. Blocage circulaire, six tentatives sans issue.

    Ce test fige la sortie : quand l'état déclaré dit que l'incomplétude est ATTENDUE,
    la sonde est sautée et le credential est persisté."""
    from oto_mcp import status_hints
    from oto_mcp.tools import zoho  # noqa: F401 — déclare l'état

    app_only = {"client_id": "1000.X", "client_secret": "s", "data_center": "eu"}
    st = status_hints.credential_state("zohodesk", app_only)
    assert st is not None and not st.complete, "l'état doit voir l'app seule"

    complete = {**app_only, "refresh_token": "1000.rt"}
    st2 = status_hints.credential_state("zohodesk", complete)
    assert st2 is not None and st2.complete, "un credential complet reste sondé"


# --- niveaux d'accès : le consentement demande les scopes du niveau en vigueur ------

def test_le_consentement_read_ne_demande_que_la_lecture():
    import urllib.parse
    url = z.build_auth_url("u1", 35, "zoho", DC, app=APP, group_id=4, level="read")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["ZohoCRM.modules.READ,ZohoCRM.settings.modules.READ,"
                          "ZohoCRM.org.READ"]
    # Le niveau et l'équipe sont SIGNÉS : le retour ne peut ni l'élargir ni changer d'app.
    etat = z.verify_state(q["state"][0])
    assert (etat["level"], etat["group"], etat["org"]) == ("read", 4, 35)


def test_au_niveau_full_l_url_est_celle_d_avant():
    import urllib.parse
    url = z.build_auth_url("u1", 35, "zohodesk", DC, app=APP, group_id=None, level="full")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == [",".join(z.SCOPES["zohodesk"])], \
        "Desk `full` a déjà `Desk.basic.READ` : la vérification n'ajoute rien"


# --- l'app : dédiée par l'équipe > dédiée par l'org > BYO > éditeur ------------------

def _apps(monkeypatch, **par_proprietaire):
    monkeypatch.setattr(z.credentials_store, "get_dedicated_app",
                        lambda t, i, c: ({"fields": par_proprietaire[t]}
                                         if t in par_proprietaire else None))


def test_l_app_dediee_de_l_equipe_prime_puis_celle_de_l_org(monkeypatch):
    def _jamais(*a, **k):
        raise AssertionError("une app dédiée trouvée : la cascade n'est pas lue")
    monkeypatch.setattr(z.access, "resolve_credential", _jamais)
    equipe = {"client_id": "1000.EQ", "client_secret": "s", "data_center": "com"}
    org = {"client_id": "1000.ORG", "client_secret": "s", "data_center": "eu"}
    _apps(monkeypatch, group=equipe, org=org)
    assert z.app_fields("zoho", "u1", "", org=35, group=4) == equipe
    assert isinstance(z.app_fields("zoho", "u1", "", org=35, group=4), z.DedicatedApp)
    assert z.app_fields("zoho", "u1", "", org=35, group=None) == org


def test_sans_app_dediee_le_byo_puis_l_editeur(monkeypatch):
    _apps(monkeypatch)
    monkeypatch.setattr(z.access, "resolve_credential", lambda *a, **k: _Resolved(APP))
    assert z.app_fields("zoho", "u1", "eu", org=35, group=4) == APP


def test_le_byo_se_resout_sous_l_org_et_l_equipe_demandees(monkeypatch):
    """Le retour n'a pas de session : la cascade du BYO doit voir l'org et l'équipe du
    state, pas l'équipe maison du compte."""
    from oto_mcp import access as acces
    _apps(monkeypatch)
    vu = {}
    monkeypatch.setattr(z.access, "resolve_credential", lambda *a, **k: vu.update(
        org=z.session_org.current_view_org(), group=z.session_org.current_view_group())
        or _Resolved({}))
    monkeypatch.setattr(z.credentials_store, "get_editor_app", lambda *a: None)
    z.app_fields("zoho", "u1", "eu", org=35, group=4)
    assert vu == {"org": 35, "group": 4}
    z.app_fields("zoho", "u1", "eu", org=35, group=None)
    assert vu == {"org": 35, "group": 0}, "sans équipe : le niveau org, pas la maison"
    assert z.session_org.current_view_org() is None, "le contexte est rendu"
    assert acces is z.access


def test_la_region_est_celle_de_l_app_dediee():
    app = z.DedicatedApp({"client_id": "a", "client_secret": "b", "data_center": "com"}, "group")
    assert z.consent_region(app, "") == "com"
    assert z.consent_region(app, "COM") == "com"
    with pytest.raises(z.ZohoOAuthError, match="data_center=com"):
        z.consent_region(app, "eu")


def test_la_region_de_la_cle_d_equipe_part_avec_le_clic():
    """L'app posée comme CLÉ (membre, équipe, org) lie la région comme l'app dédiée : le
    membre qui clique « connecter » sans région part dans celle de l'app de son équipe,
    et une autre région est refusée au lieu d'échouer chez Zoho en `invalid_client`."""
    byo = {**APP, "data_center": "eu"}
    assert z.consent_region(byo, "") == "eu"
    assert z.consent_region(byo, "EU") == "eu"
    with pytest.raises(z.ZohoOAuthError, match="data_center=eu"):
        z.consent_region(byo, "com")


def test_sans_region_nulle_part_le_refus_est_nomme():
    """Ni l'app ni la demande ne portent de région : refus NOMMÉ qui dit quoi renseigner,
    jamais une région par défaut."""
    with pytest.raises(z.ZohoRegionMissing, match="Data center"):
        z.consent_region(dict(APP), "")
    # Sans app du tout (l'app d'éditeur se choisit par région) : c'est la région du
    # compte qu'il faut choisir.
    with pytest.raises(z.ZohoRegionMissing, match="Choose the region"):
        z.consent_region({}, "")
    # L'app d'éditeur ne porte pas de région : la demandée.
    assert z.consent_region(dict(APP), "jp") == "jp"


def test_le_depart_sans_region_rend_zoho_region_missing(monkeypatch):
    """La face REST/MCP rend le code nommé, pas `zoho_oauth_unavailable`."""
    from oto_mcp.capabilities import zoho_connect
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.connectors import activation_gate
    monkeypatch.setattr(activation_gate, "exiger_connectable_capacite", lambda *a, **k: None)
    monkeypatch.setattr(zoho_connect.access, "current_group", lambda sub: 4)
    monkeypatch.setattr(z, "app_fields", lambda *a, **k: dict(APP))
    monkeypatch.setattr(z, "consent_level", lambda *a, **k: "full")

    class _Ctx:
        sub, org_id = "u1", 7
    with pytest.raises(AuthzDenied) as e:
        zoho_connect.start_for(_Ctx(), "zoho", "")
    assert e.value.code == "zoho_region_missing"


def test_le_depart_signe_le_niveau_et_l_equipe_du_contexte(monkeypatch):
    """`start_for` (MCP `zoho_connect`, carte, flux commun) résout le niveau sous l'org
    ET l'équipe active, et l'URL comme le state le portent."""
    import urllib.parse
    from types import SimpleNamespace
    from oto_mcp.capabilities import zoho_connect
    from oto_mcp.connectors import activation_gate
    monkeypatch.setattr(activation_gate, "exiger_connectable_capacite", lambda *a, **k: None)
    monkeypatch.setattr(zoho_connect.access, "current_group", lambda sub: 4)
    vu = {}
    monkeypatch.setattr(z, "consent_level",
                        lambda c, org, group: vu.update(niveau=(c, org, group)) or "read")
    monkeypatch.setattr(z, "app_fields", lambda c, sub, dc, *, org, group:
                        vu.update(app=(org, group)) or APP)
    url = zoho_connect.start_for(SimpleNamespace(sub="u1", org_id=35), "zohodesk",
                                 "eu").auth_url
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["Desk.articles.READ,Desk.search.READ,Desk.basic.READ"]
    etat = z.verify_state(q["state"][0])
    assert (etat["level"], etat["group"], etat["org"]) == ("read", 4, 35)
    assert vu == {"niveau": ("zohodesk", 35, 4), "app": (35, 4)}


# --- la région que l'app applicable LIE, servie à l'écran (`connect.fixed`) ----------

def _portee(monkeypatch, *, dediee=(), a_portee=True, byo=None, editeur=(), mode="user"):
    monkeypatch.setattr(z.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(z.access, "current_group", lambda sub: 4)
    monkeypatch.setattr(z.credentials_store, "dedicated_app_owners",
                        lambda c, org, group: list(dediee))
    monkeypatch.setattr(z.group_store, "list_groups_for_user", lambda sub, org: [])
    monkeypatch.setattr(z.access, "preloaded_presence_probe", lambda *a, **k: None)
    monkeypatch.setattr(z.chain_shadow, "resolution_rungs",
                        lambda *a, **k: iter([object()] if a_portee else []))
    monkeypatch.setattr(z.access, "resolve_credential",
                        lambda *a, **k: _Resolved(byo or {}, mode))
    monkeypatch.setattr(z.credentials_store, "list_editor_apps",
                        lambda: [{"connector": c, "data_center": "eu"} for c in editeur])
    monkeypatch.setattr(z.credentials_store, "get_editor_app", lambda *a: None)
    _apps(monkeypatch)


def test_la_cle_d_equipe_sert_sa_region(monkeypatch):
    """La clé posée par l'équipe porte sa région : l'écran la montre au lieu d'un choix
    que le départ refuserait."""
    _portee(monkeypatch, byo={**APP, "data_center": " EU "})
    assert z.app_ready("zoho", "u1") is True
    assert z.app_region("zoho", "u1") == "eu"


def test_l_app_dediee_sert_sa_region(monkeypatch):
    _portee(monkeypatch, dediee=("group",))
    _apps(monkeypatch, group={"client_id": "1000.EQ", "client_secret": "s",
                              "data_center": "com"})
    assert z.app_ready("zoho", "u1") is True
    assert z.app_region("zoho", "u1") == "com"


def test_l_app_d_editeur_ne_lie_aucune_region(monkeypatch):
    """Rien d'apporté : l'app d'oto, choisie par région au clic — le choix reste."""
    _portee(monkeypatch, a_portee=False, editeur=("zoho",))
    assert z.app_ready("zoho", "u1") is True
    assert z.app_region("zoho", "u1") == ""


def test_la_copie_de_l_app_d_oto_ne_lie_rien(monkeypatch):
    """La clé d'un consentement sur l'app d'oto : ni région imposée (le choix reste), ni
    avertissement d'organisation non vérifiée, alors que l'app reste prête."""
    _portee(monkeypatch, byo={"client_id": "oto-eu", "client_secret": "s",
                              "refresh_token": "r", "data_center": "eu"}, editeur=("zoho",))
    monkeypatch.setattr(z.credentials_store, "get_editor_app",
                        lambda c, dc: {"client_id": f"oto-{dc}", "client_secret": "s"})
    assert z.app_ready("zoho", "u1") is True
    assert z.app_region("zoho", "u1") == ""
    assert z.app_warnings("zoho", "u1") == []


def test_une_cle_sans_app_ne_lie_rien(monkeypatch):
    """Une clé à portée sans client_id/secret (consentement en cours) ne fixe rien."""
    _portee(monkeypatch, byo={"refresh_token": "r", "data_center": "eu"})
    assert z.app_ready("zoho", "u1") is False
    assert z.app_region("zoho", "u1") == ""


def test_le_flux_sert_la_region_fixee(monkeypatch):
    """`connector_flow.fixed` : la région liée, dans les options déclarées — une valeur
    inconnue ou une lecture en échec rend `{}` (le départ, lui, refuse)."""
    import oto_mcp.tools.zoho  # noqa: F401 — déclare le flux
    from oto_mcp.connectors import flow as connector_flow
    monkeypatch.setattr(z, "app_region", lambda c, sub: "eu")
    assert connector_flow.fixed("zohodesk", "u1") == {"data_center": "eu"}
    monkeypatch.setattr(z, "app_region", lambda c, sub: "")
    assert connector_flow.fixed("zoho", "u1") == {}
    monkeypatch.setattr(z, "app_region", lambda c, sub: "xx")
    assert connector_flow.fixed("zoho", "u1") == {}

    def _panne(c, sub):
        raise RuntimeError("coffre")
    monkeypatch.setattr(z, "app_region", _panne)
    assert connector_flow.fixed("zoho", "u1") == {}
    assert connector_flow.fixed("zoho", "") == {}


# --- l'organisation consentie (`zoho_org_id`) -------------------------------------

TOKENS = {"access_token": "at", "refresh_token": "rt", "api_domain": "https://www.zohoapis.eu"}
EQUIPE = {**APP, "data_center": "eu", "zoho_org_id": "20071234"}


class _Rep:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _zoho_http(monkeypatch, lu: dict, revoque: int = 200):
    """`GET` rend `lu` ; `POST` (révocation) est noté et rend `revoque`."""
    vu = {"get": [], "revoke": []}

    def _get(url, headers=None, timeout=None):
        vu["get"].append((url, headers))
        return lu if isinstance(lu, _Rep) else _Rep(lu)

    def _post(url, data=None, timeout=None, **kw):
        assert "params" not in kw, "le jeton dans le CORPS, jamais dans l'URL (#284)"
        vu["revoke"].append((url, data))
        return _Rep({}, revoque)
    monkeypatch.setattr(z.requests, "get", _get)
    monkeypatch.setattr(z.requests, "post", _post)
    return vu


def test_l_app_d_editeur_ne_verifie_rien(monkeypatch):
    """N'importe quel compte consent à l'app d'oto, c'est voulu : rien à comparer."""
    vu = _zoho_http(monkeypatch, {})
    assert z.check_org("zoho", "eu", TOKENS, z.EditorApp(APP)) == z.OrgCheck()
    assert vu == {"get": [], "revoke": []}, "l'app d'éditeur ne lit RIEN, même pour montrer"


SANS_ORG = {**APP, "data_center": "eu"}


def test_une_app_apportee_sans_org_passe_le_signale_et_montre_l_org(monkeypatch):
    """Aucune org attendue : rien à comparer, mais l'org consentie est LUE pour être
    montrée — l'identifiant affiché est le `zgid` (l'« Org ID » de Company Details)."""
    vu = _zoho_http(monkeypatch, {"org": [{"id": "5725767000000020005", "zgid": "808232144",
                                           "domain_name": "org808232144",
                                           "company_name": "Équipe"}]})
    verdict = z.check_org("zoho", "eu", TOKENS, SANS_ORG)
    assert verdict.warning == z.ORG_UNVERIFIED
    assert [(o.name, o.shown_id) for o in verdict.consented] == [("Équipe", "808232144")]
    assert verdict.consented[0].ids == {"5725767000000020005", "808232144", "org808232144"}
    assert vu["get"][0][0] == "https://www.zohoapis.eu/crm/v7/org"
    assert vu["revoke"] == [], "un avertissement ne révoque rien"


def test_le_crm_sans_zgid_montre_son_id(monkeypatch):
    _zoho_http(monkeypatch, {"org": [{"id": "57", "company_name": "Équipe"}]})
    (org,) = z.check_org("zoho", "eu", TOKENS, SANS_ORG).consented
    assert org.shown_id == "57"


def test_desk_sans_org_montre_chaque_organisation(monkeypatch):
    vu = _zoho_http(monkeypatch, {"data": [{"id": 111, "companyName": "A"},
                                           {"id": 222, "companyName": "B"}]})
    verdict = z.check_org("zohodesk", "eu", TOKENS, SANS_ORG)
    assert verdict.warning == z.ORG_UNVERIFIED
    assert [(o.name, o.shown_id) for o in verdict.consented] == [("A", "111"), ("B", "222")]
    assert vu["revoke"] == []


def test_analytics_sans_org_montre_son_org_id(monkeypatch):
    _zoho_http(monkeypatch, {})
    monkeypatch.setattr(z, "analytics_orgs", lambda f: [
        {"org_id": "60012345", "name": "B", "role": "Admin"}])
    (org,) = z.check_org("zohoanalytics", "eu", TOKENS, SANS_ORG).consented
    assert (org.name, org.shown_id) == ("B", "60012345")


@pytest.mark.parametrize("panne", ["http", "reseau"])
def test_une_lecture_en_echec_garde_l_avertissement_seul(monkeypatch, caplog, panne):
    """Mode avertissement : lire n'est que MONTRER — un échec ne révoque rien, ne refuse
    rien, garde l'avertissement seul, et se journalise sous son nom, sans jeton ni corps."""
    vu = _zoho_http(monkeypatch, _Rep({"detail": "corps-amont"}, 401))
    if panne == "reseau":
        def _coupe(url, headers=None, timeout=None):
            raise z.requests.ConnectionError(f"{url} {headers}")
        monkeypatch.setattr(z.requests, "get", _coupe)
    with caplog.at_level("WARNING", logger=z.logger.name):
        verdict = z.check_org("zoho", "eu", TOKENS, SANS_ORG)
    assert verdict == z.OrgCheck(z.ORG_UNVERIFIED, ())
    assert vu["revoke"] == []
    assert "zoho_consented_org_unread" in caplog.text
    assert "Zoho-oauthtoken" not in caplog.text and "corps-amont" not in caplog.text


@pytest.mark.parametrize("champ", ["id", "zgid", "domain_name"])
def test_le_crm_reconnait_chaque_identifiant_que_zoho_montre(monkeypatch, champ):
    org = {"id": "1", "zgid": "2", "domain_name": "org3", "company_name": "Équipe"}
    vu = _zoho_http(monkeypatch, {"org": [org]})
    assert z.check_org("zoho", "eu", TOKENS, {**EQUIPE, "zoho_org_id": org[champ]}) \
        == z.OrgCheck()
    url, entete = vu["get"][0]
    assert url == "https://www.zohoapis.eu/crm/v7/org"
    assert entete == {"Authorization": "Zoho-oauthtoken at"}
    assert vu["revoke"] == []


def test_une_autre_org_est_refusee_revoquee_et_nommee(monkeypatch):
    vu = _zoho_http(monkeypatch, {"org": [{"id": "999", "zgid": "998",
                                           "company_name": "Autre société"}]})
    with pytest.raises(z.ZohoOrgMismatch) as e:
        z.check_org("zoho", "eu", TOKENS, EQUIPE)
    assert e.value.consented == "Autre société"
    assert vu["revoke"] == [("https://accounts.zoho.eu/oauth/v2/token/revoke",
                             {"token": "rt"})]


def test_une_org_illisible_est_refusee_et_revoquee(monkeypatch):
    """`zoho_org_id` posé : la vérification existe parce qu'il l'est — illisible, on
    refuse plutôt que de ranger sans contrôle."""
    vu = _zoho_http(monkeypatch, _Rep({}, 401))
    with pytest.raises(z.ZohoOrgUnverifiable):
        z.check_org("zoho", "eu", TOKENS, EQUIPE)
    assert len(vu["revoke"]) == 1


def test_desk_compare_les_organisations_du_compte(monkeypatch):
    vu = _zoho_http(monkeypatch, {"data": [{"id": 111, "companyName": "A"},
                                           {"id": 20071234, "companyName": "B"}]})
    assert z.check_org("zohodesk", "eu", TOKENS, EQUIPE) == z.OrgCheck()
    assert vu["get"][0][0] == "https://desk.zoho.eu/api/v1/organizations"


def test_analytics_compare_ses_organisations(monkeypatch):
    vu = _zoho_http(monkeypatch, {})
    vues = {}
    monkeypatch.setattr(z, "analytics_orgs", lambda f: vues.update(f) or [
        {"org_id": "20071234", "name": "B", "role": "Admin"}])
    assert z.check_org("zohoanalytics", "eu", TOKENS, EQUIPE) == z.OrgCheck()
    assert vues["refresh_token"] == "rt" and vues["client_id"] == APP["client_id"]
    assert vu["get"] == []


def test_une_revocation_en_echec_est_journalisee_sous_son_nom(monkeypatch, caplog):
    _zoho_http(monkeypatch, {"org": [{"id": "999", "company_name": "Autre"}]}, revoque=500)
    with caplog.at_level("WARNING", logger=z.logger.name):
        with pytest.raises(z.ZohoOrgMismatch):
            z.check_org("zoho", "eu", TOKENS, EQUIPE)
    assert "zoho_revocation_failed" in caplog.text


@pytest.mark.parametrize("mode, attendu", [("user", "member"), ("group", "group"),
                                           ("org", "org"), ("tenant", "tenant")])
def test_la_carte_nomme_le_proprietaire_de_la_cle(monkeypatch, mode, attendu):
    """L'écran offre de changer SA clé, et dit à qui s'adresser pour une clé partagée."""
    _portee(monkeypatch, byo={**APP, "data_center": "eu"}, mode=mode)
    assert z.app_owner("zoho", "u1") == attendu
    assert z.app_region("zoho", "u1") == "eu"


@pytest.mark.parametrize("porteur", ["group", "org"])
def test_la_carte_nomme_le_proprietaire_de_l_app_dediee(monkeypatch, porteur):
    _portee(monkeypatch, dediee=(porteur,))
    _apps(monkeypatch, **{porteur: {"client_id": "1000.X", "client_secret": "s",
                                    "data_center": "com"}})
    assert z.app_owner("zoho", "u1") == porteur


def test_l_app_d_editeur_dit_ses_regions_et_aucune_cle_n_en_contraint(monkeypatch):
    """L'app d'oto se choisit par région : l'écran annonce celles qui existent, avant le
    clic. Une clé apportée ne dépend pas de cette liste."""
    _portee(monkeypatch, a_portee=False, editeur=("zoho",))
    assert z.app_owner("zoho", "u1") == "editor"
    assert z.app_ready_regions("zoho", "u1") == ("eu",)
    _portee(monkeypatch, byo={**APP, "data_center": "com"}, editeur=("zoho",))
    assert z.app_ready_regions("zoho", "u1") == ()


def test_sans_aucune_app_la_carte_ne_promet_rien(monkeypatch):
    _portee(monkeypatch, a_portee=False)
    assert z.app_ready("zoho", "u1") is False
    assert z.app_owner("zoho", "u1") == ""
    assert z.app_ready_regions("zoho", "u1") == ()


def test_une_copie_de_l_app_d_editeur_n_est_pas_la_cle_du_membre(monkeypatch):
    """Consentir sur l'app d'oto en range une copie : la carte ne doit pas la présenter
    comme l'application du membre."""
    _portee(monkeypatch, byo={**APP, "data_center": "eu"}, editeur=("zoho",))
    monkeypatch.setattr(z.credentials_store, "get_editor_app", lambda *a: dict(APP))
    assert z.app_owner("zoho", "u1") == "editor"
    assert z.app_region("zoho", "u1") == ""


def test_le_message_sans_app_ne_parle_pas_de_self_client():
    with pytest.raises(z.ZohoOAuthError) as e:
        z.resolve_app({})
    assert "self client" not in str(e.value)
    assert "ask an admin" in str(e.value)


def test_la_carte_signale_une_cle_d_equipe_sans_org(monkeypatch):
    _portee(monkeypatch, byo={**APP, "data_center": "eu"})
    assert z.app_warnings("zoho", "u1") == [z.ORG_UNVERIFIED]


def test_la_carte_ne_signale_rien_quand_l_org_est_nommee(monkeypatch):
    _portee(monkeypatch, byo=EQUIPE)
    assert z.app_warnings("zoho", "u1") == []


def test_la_carte_ne_signale_rien_sur_l_app_d_editeur(monkeypatch):
    _portee(monkeypatch, a_portee=False, editeur=("zoho",))
    assert z.app_warnings("zoho", "u1") == []


def test_l_app_dediee_sans_org_est_signalee(monkeypatch):
    _portee(monkeypatch, dediee=("group",))
    _apps(monkeypatch, group={"client_id": "1000.EQ", "client_secret": "s",
                              "data_center": "com"})
    assert z.app_warnings("zoho", "u1") == [z.ORG_UNVERIFIED]


def test_l_app_d_editeur_porte_sa_marque(monkeypatch):
    monkeypatch.setattr(z.credentials_store, "get_editor_app", lambda c, dc: dict(APP))
    assert isinstance(z.editor_app("zoho", "eu"), z.EditorApp)
    monkeypatch.setattr(z.credentials_store, "get_editor_app", lambda c, dc: None)
    assert z.editor_app("zoho", "eu") == {}


# --- le retour du consentement : refus nommé, avertissement nommé -----------------

_REEL = object()


def _retour(monkeypatch, verdict, app=EQUIPE, connector="zoho"):
    """La route de retour, avec un état signé et un échange stubé ; `verdict` = ce que
    rend (ou lève) `check_org`, `_REEL` = le vrai `check_org` (HTTP Zoho stubé par
    l'appelant). Rend (Location, persisté ?)."""
    import asyncio
    from starlette.requests import Request
    from oto_mcp.api import zoho as route
    ecrit = []
    monkeypatch.setattr(z, "app_fields", lambda *a, **k: dict(app))
    monkeypatch.setattr(z, "exchange_code", lambda *a, **k: dict(TOKENS))
    monkeypatch.setattr(z, "persist", lambda *a, **k: ecrit.append(1))
    monkeypatch.setattr(route.roles, "is_org_member", lambda sub, org: True)

    def _verdict(*a):
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
    if verdict is not _REEL:
        monkeypatch.setattr(z, "check_org", _verdict)
    etat = z.make_state("u1", 35, connector, "eu", group_id=4, level="read")
    (cb,) = route.make_routes(None, None, None, None, None)
    req = Request({"type": "http", "method": "GET", "path": cb.path, "headers": [],
                   "query_string": f"code=c&state={etat}".encode()})
    rep = asyncio.run(cb.endpoint(req))
    return rep.headers["location"], bool(ecrit)


def test_le_retour_d_une_autre_org_ne_range_rien_et_la_nomme(monkeypatch):
    lieu, range_ = _retour(monkeypatch, z.ZohoOrgMismatch("Autre société"))
    assert not range_
    assert "connect=zoho_org_mismatch" in lieu
    assert "consented_org=Autre+soci%C3%A9t%C3%A9" in lieu
    assert "consented_org_id" not in lieu, "le refus nomme, il ne montre pas d'identifiant"


def test_le_retour_sans_org_attendue_range_et_previent(monkeypatch):
    lieu, range_ = _retour(monkeypatch, z.OrgCheck(z.ORG_UNVERIFIED))
    assert range_
    assert "connect=connected" in lieu and "warning=zoho_org_unverified" in lieu
    assert "consented_org" not in lieu, "rien de lu : l'avertissement seul"


def _requete(lieu: str) -> list:
    from urllib.parse import parse_qsl, urlsplit
    return parse_qsl(urlsplit(lieu).query, keep_blank_values=True)


def test_le_retour_sans_org_attendue_montre_l_org_consentie_du_crm(monkeypatch):
    """De bout en bout (vrai `check_org`, HTTP Zoho stubé) : le retour MONTRE le nom et
    l'identifiant à recopier dans `zoho_org_id` — rien n'en est retenu."""
    vu = _zoho_http(monkeypatch, {"org": [{"id": "5725767000000020005", "zgid": "808232144",
                                           "domain_name": "org808232144",
                                           "company_name": "Équipe Démo"}]})
    lieu, range_ = _retour(monkeypatch, _REEL, app=SANS_ORG)
    assert range_ and vu["revoke"] == []
    q = _requete(lieu)
    assert ("connect", "connected") in q and ("warning", "zoho_org_unverified") in q
    assert [v for k, v in q if k == "consented_org"] == ["Équipe Démo"]
    assert [v for k, v in q if k == "consented_org_id"] == ["808232144"]


def test_le_retour_montre_chaque_org_de_desk_par_paires(monkeypatch):
    _zoho_http(monkeypatch, {"data": [{"id": 111, "companyName": "A"},
                                      {"id": 222, "companyName": "B"}]})
    lieu, range_ = _retour(monkeypatch, _REEL, app=SANS_ORG, connector="zohodesk")
    q = _requete(lieu)
    assert range_ and ("warning", "zoho_org_unverified") in q
    assert [v for k, v in q if k == "consented_org"] == ["A", "B"]
    assert [v for k, v in q if k == "consented_org_id"] == ["111", "222"]


def test_le_retour_dont_la_lecture_echoue_range_avec_l_avertissement_seul(monkeypatch):
    vu = _zoho_http(monkeypatch, _Rep({}, 500))
    lieu, range_ = _retour(monkeypatch, _REEL, app=SANS_ORG)
    assert range_ and vu["revoke"] == []
    assert "connect=connected" in lieu and "warning=zoho_org_unverified" in lieu
    assert "consented_org" not in lieu


def test_le_retour_verifie_ne_porte_aucun_avertissement(monkeypatch):
    lieu, range_ = _retour(monkeypatch, z.OrgCheck())
    assert range_ and "connect=connected" in lieu and "warning" not in lieu


def test_le_retour_illisible_ne_range_rien(monkeypatch):
    lieu, range_ = _retour(monkeypatch, z.ZohoOrgUnverifiable("x"))
    assert not range_ and "connect=error" in lieu

"""Custom API apps in `oauth2_user` mode (`auth/api_oauth.py`) — each member signs in at
the API's own authorization server; the vault keeps their refresh token, the call goes
out as a bearer. No database here: the vault and the row lock are replaced by doubles."""
from __future__ import annotations

import json
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from oto_mcp import egress
from oto_mcp.auth import api_oauth, flow as oauth_flow

#: The real guard, kept before the fixture replaces it.
_GARDE = egress.check_url

APP = {
    "id": 7, "org_id": 42, "account": "crm",
    "connection": {
        "base_url": "https://api.example.test/graphql", "auth_mode": "oauth2_user",
        "authorize_url": "https://auth.example.test/oidc/auth",
        "token_url": "https://auth.example.test/oidc/token",
        "oauth_client_id": "public-client", "scope": "openid offline_access read",
        "resource": "https://api.example.test",
    },
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    monkeypatch.setattr(api_oauth, "redirect",
                        lambda: "https://oto.example.test/api/custom-api/oauth/callback")
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    api_oauth._JETONS.clear()


def _params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# --- starting -------------------------------------------------------------------

def test_l_url_d_autorisation_porte_pkce_resource_et_consentement(monkeypatch):
    monkeypatch.setitem(oauth_flow.RETURN_APPS, "front-test",
                        ("https://front.example.test", "/connectors"))
    url = api_oauth.authorize_url(APP, "sub-1", "front-test")
    assert url.startswith("https://auth.example.test/oidc/auth?")
    p = _params(url)
    assert p["response_type"] == "code" and p["client_id"] == "public-client"
    assert p["code_challenge_method"] == "S256" and p["code_challenge"]
    assert p["resource"] == "https://api.example.test"
    assert p["scope"] == "openid offline_access read"
    # offline_access only yields a refresh token with an explicit consent prompt.
    assert p["prompt"] == "consent"
    assert "client_secret" not in p
    d = api_oauth.verify_state(p["state"])
    assert d["sub"] == "sub-1" and d["org"] == 42 and d["api_app"] == 7
    assert d["account"] == "crm" and d["app"] == "front-test" and d["v"]


def test_sans_offline_access_pas_de_prompt_et_sans_resource_pas_de_parametre():
    app = {**APP, "connection": {**APP["connection"], "scope": "openid", "resource": ""}}
    p = _params(api_oauth.authorize_url(app, "sub-1"))
    assert "prompt" not in p and "resource" not in p


def test_une_app_d_un_autre_mode_ou_incomplete_refuse_en_le_nommant():
    autre = {**APP, "connection": {**APP["connection"], "auth_mode": "bearer"}}
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.authorize_url(autre, "sub-1")
    assert e.value.code == "api_app_not_oauth_user"
    trou = {**APP, "connection": {**APP["connection"], "oauth_client_id": ""}}
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.authorize_url(trou, "sub-1")
    assert e.value.code == "api_app_incomplete" and "oauth_client_id" in e.value.message


def test_le_verifier_voyage_scelle_dans_le_state():
    url = api_oauth.authorize_url(APP, "sub-1")
    state = _params(url)["state"]
    brut = oauth_flow.read_state("custom_api", state)
    assert "v" not in brut and brut["vs"]
    d = api_oauth.verify_state(state)
    assert d["v"] and d["v"] not in state and "vs" not in d
    # Sealed for ITS state: under another jti it does not open, and the state is refused.
    autre = oauth_flow.sign_state("custom_api", {**{k: v for k, v in brut.items()
                                                   if k not in ("aud", "ts")},
                                                "jti": "autre-jti"})
    assert api_oauth.verify_state(autre) is None


def test_un_state_d_un_autre_flux_est_refuse():
    autre = oauth_flow.sign_state("jefacture", {"sub": "s", "org": 1, "jti": "j"})
    assert api_oauth.verify_state(autre) is None
    assert api_oauth._return_state(autre) is None


def test_le_retour_est_declare_pour_la_carte_api():
    assert oauth_flow.return_for_card("api") is api_oauth.RETURN
    assert api_oauth.RETURN.callback_path == "/api/custom-api/oauth/callback"


# --- doubles for the token server and the vault ----------------------------------

class _Rep:
    def __init__(self, status, body, *, brut: bytes | None = None):
        self.status_code, self._body = status, body
        self._brut = brut if brut is not None else json.dumps(body).encode()

    def iter_content(self, taille):
        for i in range(0, len(self._brut), taille):
            yield self._brut[i:i + taille]

    def close(self):
        pass


class _Coffre:
    """`credentials_store` as `api_oauth` uses it: one row per (type, id, account)."""

    def __init__(self):
        self.rows: dict[tuple, dict] = {}

    def set_credential(self, et, eid, connector, secret, set_by=None, meta=None,
                       account="", conn=None, **_):
        assert connector == "api"
        self.rows[(et, eid, account)] = {"secret": secret, "meta": meta or {},
                                         "set_by": set_by}

    def get_credential_with_meta(self, et, eid, connector, account=""):
        return self.rows.get((et, eid, account))


@pytest.fixture
def coffre(monkeypatch):
    c = _Coffre()
    monkeypatch.setattr(api_oauth.credentials_store, "set_credential", c.set_credential)
    monkeypatch.setattr(api_oauth.credentials_store, "get_credential_with_meta",
                        c.get_credential_with_meta)
    # `completer_la_pose` looks for an existing unbound card of the same name.
    monkeypatch.setattr("oto_mcp.db.api_apps.ligne", lambda *a: None)
    # Renewal reads the key's app (its `token_url` origin) by the row's `meta.api_app`.
    c.apps = {7: APP}
    monkeypatch.setattr("oto_mcp.db.api_apps.get", lambda i: c.apps.get(int(i)))

    @contextmanager
    def _verrou(ligne):
        yield object()
    monkeypatch.setattr(api_oauth, "_verrou_ligne", _verrou)
    monkeypatch.setattr(api_oauth.connector_health, "record_health", lambda *a, **k: None)
    marques = []
    monkeypatch.setattr(api_oauth.connector_health, "mark_rejected",
                        lambda *a, **k: marques.append(a))
    c.marques = marques
    return c


def _poste(monkeypatch, *reponses):
    envois = []
    file = list(reponses)

    def post(url, data=None, timeout=None, headers=None, **kw):
        # The token URL is the final address: never a redirect followed.
        assert kw.get("allow_redirects") is False
        envois.append((url, dict(data)))
        return file.pop(0)
    monkeypatch.setattr(api_oauth.requests, "post", post)
    return envois


# --- the exchange ------------------------------------------------------------------

def _etat(**k):
    return {"sub": "sub-1", "org": 42, "api_app": 7, "account": "crm", "v": "verif",
            "app": "", "jti": "j", **k}


def test_l_echange_envoie_le_verifier_sans_secret_et_range_la_cle_du_membre(
        monkeypatch, coffre):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: APP)
    envois = _poste(monkeypatch, _Rep(200, {"access_token": "at1", "refresh_token": "rt1",
                                            "expires_in": 3600, "scope": "read"}))
    out = api_oauth.exchange_and_persist(_etat(), "the-code")
    assert out == {"account": "crm", "scope": "member"}
    url, data = envois[0]
    assert url == APP["connection"]["token_url"]
    assert data["grant_type"] == "authorization_code" and data["code_verifier"] == "verif"
    assert data["client_id"] == "public-client" and "client_secret" not in data
    assert data["resource"] == "https://api.example.test"
    (cle, row), = coffre.rows.items()
    assert cle == ("member", "42:sub-1", "crm")
    champs = json.loads(row["secret"])
    assert champs["refresh_token"] == "rt1" and champs["auth_mode"] == "oauth2_user"
    assert row["meta"]["api_app"] == 7
    # The access token stays in the process cache, never in the vault.
    assert "at1" not in row["secret"]
    assert api_oauth.access_token_for(cle, champs) == "at1"


def test_sans_refresh_token_rien_n_est_range(monkeypatch, coffre):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: APP)
    _poste(monkeypatch, _Rep(200, {"access_token": "at1", "expires_in": 3600}))
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        api_oauth.exchange_and_persist(_etat(), "c")
    assert e.value.code == "no_refresh_token" and not coffre.rows


def test_une_app_remplacee_pendant_la_connexion_refuse(monkeypatch, coffre):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante",
                        lambda org, account: {**APP, "id": 8})
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        api_oauth.exchange_and_persist(_etat(), "c")
    assert e.value.code == "api_app_removed"


def test_le_jeton_s_echange_sans_suivre_de_redirection(monkeypatch, coffre):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: APP)
    envois = _poste(monkeypatch, _Rep(302, {}))
    with pytest.raises(oauth_flow.OAuthExchangeRefused) as e:
        api_oauth.exchange_and_persist(_etat(), "c")
    assert "redirect" in str(e.value) and len(envois) == 1 and not coffre.rows


def test_une_reponse_de_jeton_trop_longue_n_est_pas_lue(monkeypatch, coffre):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: APP)
    trop = b'{"access_token": "' + b"a" * (api_oauth._MAX_TOKEN_BODY + 1) + b'"}'
    _poste(monkeypatch, _Rep(200, None, brut=trop))
    with pytest.raises(oauth_flow.OAuthExchangeRefused) as e:
        api_oauth.exchange_and_persist(_etat(), "c")
    assert "KB" in str(e.value) and not coffre.rows


def test_un_serveur_injoignable_est_un_refus_nomme(monkeypatch, coffre):
    champs = _pose(coffre)

    def post(*a, **k):
        raise api_oauth.requests.ConnectionError("x")
    monkeypatch.setattr(api_oauth.requests, "post", post)
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.access_token_for(LIGNE, champs)
    assert e.value.code == "api_oauth_renewal_refused" and not coffre.marques


# --- renewal -----------------------------------------------------------------------

LIGNE = ("member", "42:sub-1", "crm")


def _pose(coffre, rt="rt1"):
    champs = {**APP["connection"], "refresh_token": rt}
    coffre.rows[LIGNE] = {"secret": json.dumps(champs), "meta": {"api_app": 7},
                          "set_by": "sub-1"}
    return champs


def test_le_renouvellement_ecrit_la_rotation_et_sert_le_nouveau_jeton(monkeypatch, coffre):
    champs = _pose(coffre)
    envois = _poste(monkeypatch, _Rep(200, {"access_token": "at2", "refresh_token": "rt2",
                                            "expires_in": 3600}))
    assert api_oauth.access_token_for(LIGNE, champs) == "at2"
    assert envois[0][1]["grant_type"] == "refresh_token"
    assert envois[0][1]["resource"] == "https://api.example.test"
    assert json.loads(coffre.rows[LIGNE]["secret"])["refresh_token"] == "rt2"
    assert coffre.rows[LIGNE]["meta"] == {"api_app": 7}   # meta kept, never wiped
    # Served from the cache now (with the ROTATED token): no second request.
    assert api_oauth.access_token_for(LIGNE, {**champs, "refresh_token": "rt2"}) == "at2"
    assert len(envois) == 1


def test_un_grant_mort_marque_la_ligne_et_demande_de_se_reconnecter(monkeypatch, coffre):
    champs = _pose(coffre)
    _poste(monkeypatch, _Rep(400, {"error": "invalid_grant"}))
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.access_token_for(LIGNE, champs)
    assert e.value.code == "api_oauth_reconnect" and "Connect" in e.value.message
    assert coffre.marques and LIGNE[0] in coffre.marques[0]
    assert LIGNE in coffre.rows    # marked, never purged


def test_un_refus_de_config_ne_marque_rien(monkeypatch, coffre):
    champs = _pose(coffre)
    _poste(monkeypatch, _Rep(401, {"error": "invalid_client"}))
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.access_token_for(LIGNE, champs)
    assert e.value.code == "api_oauth_renewal_refused" and not coffre.marques


def test_un_jeton_change_pendant_le_refus_est_une_reconnexion_rejouee(monkeypatch, coffre):
    champs = _pose(coffre)
    reponses = [_Rep(400, {"error": "invalid_grant"}),
                _Rep(200, {"access_token": "at3", "expires_in": 3600})]

    def post(url, data=None, timeout=None, headers=None, **kw):
        rep = reponses.pop(0)
        if rep.status_code == 400:      # the person reconnected meanwhile
            _pose(coffre, rt="rt-neuf")
        return rep
    monkeypatch.setattr(api_oauth.requests, "post", post)
    assert api_oauth.access_token_for(LIGNE, champs) == "at3"
    assert not coffre.marques


# --- the call: a bearer for oto-core ---------------------------------------------

def test_l_appel_part_en_bearer_avec_le_jeton_du_membre(monkeypatch, coffre):
    from oto_mcp.tools import api as api_tool
    champs = _pose(coffre)
    _poste(monkeypatch, _Rep(200, {"access_token": "at-call", "expires_in": 3600}))
    client = api_tool.client_for(champs, row=LIGNE)
    assert type(client._auth).__name__ == "StaticBearer"
    assert "at-call" in vars(client._auth).values()


def test_sans_ligne_le_mode_refuse():
    from oto_mcp.tools import api as api_tool
    with pytest.raises(ValueError):
        api_tool.client_for({**APP["connection"], "refresh_token": "rt"}, row=None)


# --- the whole return: callback, then the front completes (card `api`) ------------

@pytest.fixture
def retour(monkeypatch, coffre):
    """The member is still allowed on return; the app is the one of the state."""
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: APP)
    permis = {"ok": True}
    monkeypatch.setattr("oto_mcp.roles.is_org_member", lambda sub, org: permis["ok"])
    monkeypatch.setattr("oto_mcp.connectors.cle_perso.interdite", lambda org, c: False)
    monkeypatch.setattr("oto_mcp.connectors.activation.cran_qui_coupe", lambda *a: None)
    return permis


def _s256(verifier: str) -> str:
    import base64
    import hashlib
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_du_callback_a_la_completion_la_cle_du_membre_est_rangee_une_fois(
        monkeypatch, coffre, retour):
    from _retour_oauth import terminer
    envois = _poste(monkeypatch, _Rep(200, {"access_token": "at", "refresh_token": "rt",
                                            "expires_in": 3600}))
    p = _params(api_oauth.authorize_url(APP, "sub-1"))
    issue, out = terminer(api_oauth.CALLBACK_PATH, {"code": "le-code", "state": p["state"]})
    assert issue == "connected", out
    assert (out["connector"], out["account"], out["scope"]) == ("api", "crm", "member")
    (_, data), = envois
    assert data["code"] == "le-code"
    assert _s256(data["code_verifier"]) == p["code_challenge"]
    assert ("member", "42:sub-1", "crm") in coffre.rows
    # Used once: the same link does not complete twice.
    issue, _ = terminer(api_oauth.CALLBACK_PATH, {"code": "le-code", "state": p["state"]})
    assert issue == "state_used" and len(envois) == 1


def test_la_completion_n_aboutit_que_pour_qui_a_lance_la_connexion(
        monkeypatch, coffre, retour):
    from _retour_oauth import terminer
    envois = _poste(monkeypatch)
    state = _params(api_oauth.authorize_url(APP, "sub-1"))["state"]
    issue, _ = terminer(api_oauth.CALLBACK_PATH, {"code": "c", "state": state},
                        sub="sub-2")
    assert issue == "state_not_yours" and not envois and not coffre.rows


def test_un_membre_sorti_de_l_org_ne_termine_pas(monkeypatch, coffre, retour):
    from _retour_oauth import terminer
    envois = _poste(monkeypatch)
    retour["ok"] = False
    state = _params(api_oauth.authorize_url(APP, "sub-1"))["state"]
    issue, _ = terminer(api_oauth.CALLBACK_PATH, {"code": "c", "state": state})
    assert issue == "not_allowed" and not envois and not coffre.rows


def test_un_refus_au_retour_ne_range_rien(monkeypatch, coffre, retour):
    from _retour_oauth import terminer
    state = _params(api_oauth.authorize_url(APP, "sub-1"))["state"]
    issue, _ = terminer(api_oauth.CALLBACK_PATH, {"error": "access_denied",
                                                  "state": state})
    assert issue == "denied" and not coffre.rows


# --- a token URL the remote server announced: no declared egress exception --------

INTERNE = "https://auth.interne.test/oidc/token"
DECOUVERTE = {**APP, "connection": {**APP["connection"], "token_url": INTERNE,
                                     "discovered": ["authorize_url", "token_url"]}}
SAISIE = {**APP, "connection": {**APP["connection"], "token_url": INTERNE}}


@pytest.fixture
def pont_declare(monkeypatch, dns_simule):
    """`auth.interne.test` is internal, reachable only through a declared exception."""
    monkeypatch.setattr("oto_mcp.egress.check_url", _GARDE)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://oto.example.test")
    monkeypatch.setenv("OTO_EGRESS_ALLOW", "pont=10.0.0.7:443")
    dns_simule.update({"auth.interne.test": {"10.0.0.7"}})


def test_un_echange_vers_un_token_url_annonce_ne_prend_pas_d_exception(
        monkeypatch, coffre, pont_declare):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: DECOUVERTE)
    envois = _poste(monkeypatch)
    with pytest.raises(oauth_flow.OAuthExchangeRefused) as e:
        api_oauth.exchange_and_persist(_etat(), "c")
    assert "token_url" in str(e.value) and not envois and not coffre.rows


def test_un_token_url_saisi_par_l_admin_garde_l_exception_declaree(
        monkeypatch, coffre, pont_declare):
    monkeypatch.setattr("oto_mcp.db.api_apps.vivante", lambda org, account: SAISIE)
    envois = _poste(monkeypatch, _Rep(200, {"access_token": "at", "refresh_token": "rt",
                                            "expires_in": 3600}))
    assert api_oauth.exchange_and_persist(_etat(), "c")["account"] == "crm"
    assert envois[0][0] == INTERNE


def test_un_renouvellement_vers_un_token_url_annonce_ne_prend_pas_d_exception(
        monkeypatch, coffre, pont_declare):
    coffre.apps[7] = DECOUVERTE
    champs = {**_pose(coffre), "token_url": INTERNE}
    envois = _poste(monkeypatch)
    with pytest.raises(api_oauth.ApiOAuthRefused) as e:
        api_oauth.access_token_for(LIGNE, champs)
    assert e.value.code == "api_oauth_renewal_refused"
    assert not envois and not coffre.marques     # refused before sending, nothing marked


def test_un_renouvellement_vers_un_token_url_saisi_garde_l_exception(
        monkeypatch, coffre, pont_declare):
    coffre.apps[7] = SAISIE
    champs = {**_pose(coffre), "token_url": INTERNE}
    envois = _poste(monkeypatch, _Rep(200, {"access_token": "at", "expires_in": 3600}))
    assert api_oauth.access_token_for(LIGNE, champs) == "at" and len(envois) == 1

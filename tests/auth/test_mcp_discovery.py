"""Signing in to a remote MCP server like Claude does (`auth/mcp_discovery.py`): the 401
names the resource metadata, which names the authorization server, whose metadata says
how a client identifies itself — no key pasted anywhere."""
from __future__ import annotations

import json

import pytest

from oto_mcp.auth import mcp_discovery as d

MCP = "https://mcp.example.test/mcp"
PRM = {"resource": MCP, "authorization_servers": ["https://auth.example.test/oidc"],
       "scopes_supported": ["openid", "profile"]}
ASM = {"issuer": "https://auth.example.test/oidc",
       "authorization_endpoint": "https://auth.example.test/oidc/auth",
       "token_endpoint": "https://auth.example.test/oidc/token",
       "code_challenge_methods_supported": ["S256"],
       "scopes_supported": ["openid", "profile", "offline_access"],
       "client_id_metadata_document_supported": True}


class _Rep:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status_code, self.headers = status, headers or {}
        self._raw = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else b"")
        self.lu = False

    def iter_content(self, n):
        self.lu = True
        for i in range(0, len(self._raw), n):
            yield self._raw[i:i + n]

    def close(self):
        pass

    def json(self):
        return json.loads(self._raw)


@pytest.fixture
def web(monkeypatch):
    pages: dict[str, _Rep] = {}
    posts: list[tuple[str, dict]] = []
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    monkeypatch.setattr("oto_mcp.auth.api_oauth.redirect",
                        lambda: "https://oto.example.test/api/custom-api/oauth/callback")
    monkeypatch.setattr(d.oauth_flow, "redirect_uri",
                        lambda path, **k: f"https://oto.example.test{path}")

    def get(url, **k):
        assert k.get("allow_redirects") is False
        return pages.get(url, _Rep(404))

    def post(url, **k):
        assert k.get("allow_redirects") is False
        posts.append((url, k))
        if url == MCP:
            return _Rep(401, headers={"WWW-Authenticate": 'Bearer resource_metadata='
                                      '"https://mcp.example.test/.well-known/'
                                      'oauth-protected-resource/mcp"'})
        return pages.get("POST " + url, _Rep(404))
    web = type("W", (), {})()
    web.pages, web.posts, web.get, web.post = pages, posts, get, post
    web.politiques = []

    class _Session:
        """The pinned session (`egress_transport`) stands here for the network; the
        policy each request was sent under is recorded."""

        def __init__(self, what, annonce):
            web.politiques.append((what, annonce))

        def get(self, url, **k):
            return web.get(url, **k)

        def post(self, url, **k):
            return web.post(url, **k)
    monkeypatch.setattr(d, "_session", lambda what, *, annonce: _Session(what, annonce))
    pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = _Rep(body=PRM)
    pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body=ASM)
    return web


def test_la_decouverte_suit_le_401_puis_les_metadonnees(web):
    m = d.discover(MCP)
    assert m["resource"] == MCP and m["issuer"] == "https://auth.example.test/oidc"
    assert m["authorize_url"].endswith("/oidc/auth") and m["token_url"].endswith("/oidc/token")
    assert m["scope"] == "openid profile offline_access"   # offline_access asked for
    assert m["cimd"] is True


def test_sans_en_tete_les_chemins_connus_suffisent(web, monkeypatch):
    web.post = lambda url, **k: _Rep(401)
    assert d.discover(MCP)["token_url"].endswith("/oidc/token")


def test_un_serveur_sans_metadonnees_refuse_en_le_nommant(web):
    web.pages.clear()
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert e.value.code == "mcp_discovery_failed" and "RFC 9728" in e.value.message


def test_un_serveur_sans_pkce_s256_est_refuse(web):
    web.pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body={**ASM, "code_challenge_methods_supported": ["plain"]})
    with pytest.raises(d.DiscoveryRefused):
        d.discover(MCP)


def test_un_serveur_ouvert_le_dit(web, monkeypatch):
    web.post = lambda url, **k: _Rep(200, body={})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert e.value.code == "mcp_no_auth_needed"


def test_cimd_donne_notre_document_comme_identifiant():
    assert d.client_id_for({"cimd": True}) == d.client_document_url()


def test_le_document_client_est_public_et_sans_secret(web):
    doc = d.client_document()
    assert doc["client_id"] == "https://oto.example.test/api/custom-api/oauth/client.json"
    assert doc["token_endpoint_auth_method"] == "none"
    assert doc["redirect_uris"] == ["https://oto.example.test/api/custom-api/oauth/callback"]


def test_sans_cimd_l_inscription_dynamique_est_un_client_public(web):
    web.pages["POST https://auth.example.test/oauth/register"] = _Rep(201, body={
        "client_id": "dyn-1"})
    cid = d.client_id_for({"cimd": False,
                           "registration_endpoint": "https://auth.example.test/oauth/register"})
    assert cid == "dyn-1"
    body = web.posts[-1][1]["json"]
    assert body["token_endpoint_auth_method"] == "none" and "client_secret" not in body


def test_ni_cimd_ni_inscription_demande_un_client_a_l_admin():
    with pytest.raises(d.DiscoveryRefused) as e:
        d.client_id_for({"cimd": False, "registration_endpoint": None})
    assert e.value.code == "mcp_client_required"


def test_completer_l_app_ne_remplit_que_le_vide_et_l_ecrit_une_fois(web, monkeypatch):
    ecrits = []
    monkeypatch.setattr("oto_mcp.db.api_apps.modifier",
                        lambda app_id, connection=None, by=None, **k:
                        ecrits.append(connection) or {"id": app_id, "connection": connection})
    app = {"id": 3, "connection": {"base_url": MCP, "protocol": "mcp",
                                   "auth_mode": "oauth2_user", "scope": "openid crm.read"}}
    out = d.complete_app(app, "sub-1")
    conn = out["connection"]
    assert conn["oauth_client_id"] == d.client_document_url()
    assert conn["scope"] == "openid crm.read"              # the admin's, kept
    assert conn["resource"] == MCP and len(ecrits) == 1
    assert conn["issuer"] == "https://auth.example.test/oidc"
    assert conn["iss_parameter_supported"] is False
    # What the remote server announced stays marked as such; the admin's scope is not.
    assert conn["discovered"] == ["authorize_url", "oauth_client_id", "resource",
                                  "token_url"]
    assert d.complete_app(out, "sub-1") is out            # complete: no second discovery


def test_une_app_mcp_peut_naitre_avec_sa_seule_adresse(monkeypatch):
    from oto_mcp.connectors import api_apps
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    out = api_apps.valider_connexion({"base_url": MCP, "protocol": "mcp",
                                      "auth_mode": "oauth2_user"})
    assert out["protocol"] == "mcp" and "authorize_url" not in out
    with pytest.raises(api_apps.AppRefused):
        api_apps.valider_connexion({"base_url": "https://api.example.test",
                                    "auth_mode": "oauth2_user"})


def test_une_ressource_d_un_autre_serveur_est_refusee(web):
    web.pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = \
        _Rep(body={**PRM, "resource": "https://mcp.autre-serveur.test/mcp"})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert "another resource" in e.value.message


def test_des_metadonnees_d_un_autre_emetteur_sont_ignorees(web):
    web.pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body={**ASM, "issuer": "https://auth.autre-serveur.test/oidc"})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert e.value.code == "mcp_discovery_failed"


def test_changer_de_serveur_efface_la_connexion_decouverte():
    from oto_mcp.capabilities.connectors import api_apps as cap
    old = {"base_url": MCP, "protocol": "mcp", "auth_mode": "oauth2_user",
           "token_url": "https://auth.example.test/oidc/token"}
    assert cap._rediscover(old, {"base_url": "https://autre.example.test/mcp"})
    assert not cap._rediscover(old, {"base_url": MCP + "/"})
    assert not cap._rediscover({**old, "protocol": "http"},
                               {"base_url": "https://autre.example.test/mcp"})


def test_le_document_client_est_servi_sans_authentification(web, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import oauth_returns
    app = Starlette(routes=oauth_returns.make_routes())
    r = TestClient(app).get(d.CLIENT_DOCUMENT_PATH)
    assert r.status_code == 200
    assert r.json()["client_id"] == d.client_document_url()


def test_un_serveur_mcp_qui_republie_son_fournisseur_d_identite_est_admis(web):
    """Le serveur se nomme lui-même serveur d'autorisation et republie les métadonnées
    de son Logto : l'émetteur diffère, l'hôte est le sien — admis."""
    web.pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = \
        _Rep(body={**PRM, "authorization_servers": ["https://mcp.example.test"]})
    web.pages["https://mcp.example.test/.well-known/oauth-authorization-server"] = \
        _Rep(body={**ASM, "client_id_metadata_document_supported": None})
    m = d.discover(MCP)
    assert m["token_url"] == "https://auth.example.test/oidc/token" and m["cimd"] is False


def test_seules_les_adresses_saisies_par_l_admin_gardent_les_exceptions_d_egress(
        web, monkeypatch):
    vus = []
    monkeypatch.setattr("oto_mcp.egress.check_url",
                        lambda url, **k: vus.append((url, k["exceptions_declarees"])))
    d.discover(MCP)
    politique = dict(vus)
    assert politique[MCP] is True                      # typed by the admin
    # Announced by the remote server: its metadata, its sign-in server's endpoints.
    assert politique["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] \
        is False
    assert politique["https://auth.example.test/oidc/token"] is False
    assert politique["https://auth.example.test/oidc/auth"] is False


def test_un_chemin_connu_sur_l_hote_saisi_garde_les_exceptions(web, monkeypatch):
    vus = []
    monkeypatch.setattr("oto_mcp.egress.check_url",
                        lambda url, **k: vus.append((url, k["exceptions_declarees"])))
    web.post = lambda url, **k: _Rep(401)
    d.discover(MCP)
    assert ("https://mcp.example.test/.well-known/oauth-protected-resource/mcp",
            True) in vus


def test_le_corps_de_l_initialize_n_est_jamais_lu(web, monkeypatch):
    rep = _Rep(401, raw=b"x" * (d._MAX * 4), headers={
        "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.test/'
                            '.well-known/oauth-protected-resource/mcp"'})
    web.post = lambda url, **k: rep
    assert d.discover(MCP)["resource"] == MCP and not rep.lu


def test_une_inscription_trop_longue_n_est_pas_lue(web):
    web.pages["POST https://auth.example.test/oauth/register"] = _Rep(
        201, raw=b'{"client_id": "' + b"a" * d._MAX + b'"}')
    with pytest.raises(d.DiscoveryRefused) as e:
        d.client_id_for({"cimd": False,
                         "registration_endpoint": "https://auth.example.test/oauth/register"})
    assert e.value.code == "mcp_registration_refused"


def test_un_refus_d_egress_pendant_la_decouverte_remonte(web, monkeypatch):
    from oto_mcp import egress

    def check(url, **k):
        if "auth.example.test" in url:
            raise egress.EgressRefused("connecteur `api` : destination interne refusée.")
    monkeypatch.setattr("oto_mcp.egress.check_url", check)
    with pytest.raises(egress.EgressRefused):
        d.discover(MCP)


def test_un_champ_decouvert_que_l_admin_change_ou_vide_n_est_plus_marque(monkeypatch):
    from oto_mcp.connectors import api_apps
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    base = {"base_url": MCP, "protocol": "mcp", "auth_mode": "oauth2_user",
            "authorize_url": "https://auth.example.test/oidc/auth",
            "token_url": "https://auth.example.test/oidc/token",
            "oauth_client_id": "cid", "discovered": ["authorize_url", "token_url"]}
    garde = api_apps.valider_connexion({"scope": "openid"}, base=base)
    assert garde["discovered"] == ["authorize_url", "token_url"]
    meme = api_apps.valider_connexion({"token_url": base["token_url"]}, base=base)
    assert meme["discovered"] == ["authorize_url", "token_url"]
    saisi = api_apps.valider_connexion(
        {"token_url": "https://auth.example.test/autre/token"}, base=base)
    assert saisi["discovered"] == ["authorize_url"]
    vide = api_apps.valider_connexion({"token_url": "", "authorize_url": ""}, base=base)
    assert "discovered" not in vide
    with pytest.raises(api_apps.AppRefused):    # never written through a route
        api_apps.valider_connexion({"discovered": ["token_url"]}, base=base)


# --- one sign-in server, one resource ------------------------------------------------

def test_chaque_requete_part_sur_une_session_gardee_avec_sa_politique(web):
    d.discover(MCP)
    # The address typed by the admin keeps the platform's exceptions; what the remote
    # server announced gets none.
    assert ("base url", False) in web.politiques
    assert ("resource metadata", True) in web.politiques
    assert ("authorization server metadata", True) in web.politiques


def test_des_points_hors_de_l_origine_de_l_emetteur_sont_refuses(web):
    web.pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body={**ASM, "token_endpoint": "https://collecte.example.test/token"})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert e.value.code == "mcp_discovery_failed" and "origin" in e.value.message


def test_un_serveur_qui_republie_son_fournisseur_garde_l_origine_de_l_emetteur_annonce(web):
    web.pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = \
        _Rep(body={**PRM, "authorization_servers": ["https://mcp.example.test"]})
    web.pages["https://mcp.example.test/.well-known/oauth-authorization-server"] = \
        _Rep(body={**ASM, "authorization_endpoint": "https://autre.example.test/auth"})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert "origin" in e.value.message


def test_l_emetteur_et_son_annonce_d_iss_sont_releves(web):
    web.pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body={**ASM, "authorization_response_iss_parameter_supported": True})
    m = d.discover(MCP)
    assert m["issuer"] == ASM["issuer"] and m["iss_parameter_supported"] is True


def test_la_ressource_est_l_adresse_canonique_du_serveur(web):
    web.pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = \
        _Rep(body={**PRM, "resource": "https://MCP.example.test:443/mcp/"})
    assert d.discover(MCP)["resource"] == MCP


def test_le_document_client_nomme_oto(web):
    assert d.client_document()["client_name"] == "oto"


def test_completer_refuse_un_point_saisi_sur_une_autre_origine_avant_toute_inscription(
        web, monkeypatch):
    monkeypatch.setattr("oto_mcp.db.api_apps.modifier",
                        lambda *a, **k: pytest.fail("nothing is written"))
    monkeypatch.setattr(d, "client_id_for",
                        lambda meta: pytest.fail("no registration with a mixed origin"))
    app = {"id": 3, "connection": {"base_url": MCP, "protocol": "mcp",
                                   "auth_mode": "oauth2_user",
                                   "token_url": "https://collecte.example.test/token"}}
    with pytest.raises(d.DiscoveryRefused) as e:
        d.complete_app(app, "sub-1")
    assert "origin" in e.value.message


def _conn(**k):
    return {"base_url": "https://api.example.test", "auth_mode": "oauth2_user",
            "authorize_url": "https://auth.example.test/oidc/auth",
            "token_url": "https://auth.example.test/oidc/token",
            "oauth_client_id": "cid", **k}


def test_l_enregistrement_refuse_des_points_oauth_sur_deux_origines(monkeypatch):
    from oto_mcp.connectors import api_apps
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    with pytest.raises(api_apps.AppRefused) as e:
        api_apps.valider_connexion(_conn(token_url="https://collecte.example.test/token"))
    assert e.value.code == "invalid_app_connection" and "same origin" in e.value.message
    with pytest.raises(api_apps.AppRefused):     # another port is another origin
        api_apps.valider_connexion(_conn(token_url="https://auth.example.test:8443/t"))
    assert api_apps.valider_connexion(_conn())["token_url"].endswith("/oidc/token")


def test_en_mcp_la_ressource_est_l_adresse_du_serveur(monkeypatch):
    from oto_mcp.connectors import api_apps
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    mcp = {"base_url": MCP, "protocol": "mcp", "auth_mode": "oauth2_user"}
    with pytest.raises(api_apps.AppRefused) as e:
        api_apps.valider_connexion({**mcp, "resource": "https://api.autre.test"})
    assert "resource" in e.value.message
    ok = api_apps.valider_connexion({**mcp, "resource": "https://MCP.example.test/mcp/"})
    assert ok["resource"] == "https://MCP.example.test/mcp/"


def test_l_emetteur_decouvert_tombe_quand_un_point_ou_l_adresse_change(monkeypatch):
    from oto_mcp.connectors import api_apps
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)
    base = {**_conn(), "issuer": "https://auth.example.test/oidc",
            "iss_parameter_supported": True}
    assert api_apps.valider_connexion({"scope": "openid"}, base=base)["issuer"]
    # Points off the recorded issuer's origin are refused…
    with pytest.raises(api_apps.AppRefused):
        api_apps.valider_connexion({"scope": "openid"},
                                   base={**base, "issuer": "https://autre.example.test"})
    # …and once the admin declares both points anew, the record no longer applies.
    deux = api_apps.valider_connexion({"authorize_url": "https://autre.example.test/a",
                                       "token_url": "https://autre.example.test/t"},
                                      base=base)
    assert "issuer" not in deux and "iss_parameter_supported" not in deux

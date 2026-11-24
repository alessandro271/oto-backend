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
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._raw = json.dumps(body).encode() if body is not None else b""

    def iter_content(self, n):
        yield self._raw

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
    monkeypatch.setattr(d.requests, "get", get)
    monkeypatch.setattr(d.requests, "post", post)
    pages["https://mcp.example.test/.well-known/oauth-protected-resource/mcp"] = _Rep(body=PRM)
    pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body=ASM)
    web = type("W", (), {})()
    web.pages, web.posts = pages, posts
    return web


def test_la_decouverte_suit_le_401_puis_les_metadonnees(web):
    m = d.discover(MCP)
    assert m["resource"] == MCP and m["issuer"] == "https://auth.example.test/oidc"
    assert m["authorize_url"].endswith("/oidc/auth") and m["token_url"].endswith("/oidc/token")
    assert m["scope"] == "openid profile offline_access"   # offline_access asked for
    assert m["cimd"] is True


def test_sans_en_tete_les_chemins_connus_suffisent(web, monkeypatch):
    monkeypatch.setattr(d.requests, "post", lambda url, **k: _Rep(401))
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
    monkeypatch.setattr(d.requests, "post", lambda url, **k: _Rep(200, body={}))
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
        _Rep(body={**PRM, "resource": "https://mcp.victime.test/mcp"})
    with pytest.raises(d.DiscoveryRefused) as e:
        d.discover(MCP)
    assert "another resource" in e.value.message


def test_des_metadonnees_d_un_autre_emetteur_sont_ignorees(web):
    web.pages["https://auth.example.test/.well-known/oauth-authorization-server/oidc"] = \
        _Rep(body={**ASM, "issuer": "https://auth.victime.test/oidc"})
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

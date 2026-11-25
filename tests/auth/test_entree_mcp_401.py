"""Face MCP : un jeton émis avant la fermeture des sessions de son compte reçoit un VRAI
401 HTTP (`auth.entree_mcp`), et la garde reprend l'état lu au lieu de le relire.

Deux bancs :

- le serveur RÉEL (`server._build_mcp` + `http_app()` + les middlewares ASGI de `main`)
  pour le 401 : il tombe avant toute la chaîne MCP, donc sans base ;
- un serveur MINIMAL (le même fournisseur d'auth, la même garde, un outil) pour le
  chemin admis : un `POST /mcp` = une lecture d'entrée, la garde n'en refait pas.

`db.get_suspension` est remplacée par un compteur : c'est l'unique lecture que la garde
d'identité fait d'un compte, et ce banc compte ses appels.
"""
from __future__ import annotations

import time

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.testclient import TestClient

from oto_mcp.auth import entree_mcp

_SUB = "banc-revocation"
_JWT_AVANT, _JWT_APRES, _OTO_AVANT, _OTO_APRES = "jwt-avant", "jwt-apres", "oto_avant", "oto_apres"
_FERME_LE = time.time() - 600          # la fermeture : il y a dix minutes

_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "banc", "version": "0"}},
}
_ENTETES = {"accept": "application/json, text/event-stream",
            "content-type": "application/json", "host": "mcp.oto.cx"}


class _Verificateur(TokenVerifier):
    """Quatre jetons : JWT (`iat`) et `oto_` (`cree_le`), émis avant ou après la fermeture."""

    async def verify_token(self, token: str) -> "AccessToken | None":
        avant, apres = _FERME_LE - 60, _FERME_LE + 60
        if token in (_JWT_AVANT, _JWT_APRES):
            iat = int(avant if token == _JWT_AVANT else apres)
            return AccessToken(token=token, client_id="banc", scopes=[], expires_at=None,
                               claims={"sub": _SUB, "iat": iat})
        if token in (_OTO_AVANT, _OTO_APRES):
            cree = avant if token == _OTO_AVANT else apres
            return AccessToken(token=token, client_id="oto_api_token", scopes=[],
                               expires_at=None, claims={"sub": _SUB, "cree_le": cree})
        return None


@pytest.fixture
def lectures(monkeypatch):
    """Compte les lectures d'entrée ; le compte a une fermeture posée, pas de pause."""
    appels: list[str] = []

    def _get_suspension(sub):
        appels.append(sub)
        return {"sub": sub, "suspended_at": None, "revoque_avant": _FERME_LE}

    from oto_mcp import db, tenant_desactive
    from oto_mcp.auth import hooks
    monkeypatch.setattr(db, "get_suspension", _get_suspension)
    monkeypatch.setattr(tenant_desactive, "refus", lambda sub: None)
    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: False)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.cx")
    return appels


def _app_reelle():
    """L'app authentifiée telle que `main` l'assemble (hors REST, sans objet ici)."""
    from oto_mcp import server
    from oto_mcp.auth import decouverte
    from oto_mcp.mcp_accueil import McpAccueilMiddleware
    mcp = server._build_mcp("streamable_http", _Verificateur())
    app = mcp.http_app()
    for route in reversed(decouverte.make_routes("https://mcp.oto.cx")):
        app.router.routes.insert(0, route)
    app.add_middleware(McpAccueilMiddleware)
    app.add_middleware(server.TenantChallengeMiddleware)
    return app


def _app_minimale():
    from pydantic import AnyHttpUrl

    from oto_mcp.middleware.account_suspended import AccountSuspendedMiddleware
    auth = entree_mcp.FournisseurOto(
        token_verifier=_Verificateur(),
        authorization_servers=[AnyHttpUrl("https://auth.oto.cx/oidc")],
        base_url="https://mcp.oto.cx")
    srv = FastMCP("banc-entree", auth=auth)
    srv.add_middleware(AccountSuspendedMiddleware())

    @srv.tool
    def ping() -> str:
        return "pong"

    return srv.http_app()


def _post(client, jeton, corps=_INITIALIZE, **entetes):
    return client.post("/mcp", json=corps,
                       headers={**_ENTETES, "authorization": f"Bearer {jeton}", **entetes})


# --- émis avant : le 401 HTTP ----------------------------------------------------

@pytest.mark.parametrize("jeton", [_JWT_AVANT, _OTO_AVANT], ids=["jwt", "oto_"])
def test_emis_avant_la_fermeture_recoit_un_vrai_401(lectures, jeton):
    r = _post(TestClient(_app_reelle()), jeton)
    assert r.status_code == 401, r.text
    defi = r.headers["www-authenticate"]
    assert defi.startswith("Bearer ")
    assert 'error="invalid_token"' in defi
    assert f'error_description="{entree_mcp.DESCRIPTION}"' in defi
    corps = r.json()
    assert corps["code"] == "session_revoked"
    assert corps["error"] == "invalid_token"
    assert lectures == [_SUB], "une lecture d'entrée, et une seule"


def test_le_challenge_porte_le_meme_resource_metadata_que_les_autres_401(lectures):
    """Même pointeur de découverte que le 401 d'un jeton invalide : le client relance
    l'OAuth au même endroit."""
    app = _app_reelle()
    revoque = _post(TestClient(app), _JWT_AVANT).headers["www-authenticate"]
    invalide = _post(TestClient(app), "faux").headers["www-authenticate"]

    def _metadata(defi):
        return [p.strip() for p in defi.split(",") if "resource_metadata=" in p]

    assert _metadata(invalide), invalide
    assert _metadata(revoque) == _metadata(invalide)


def test_la_description_respecte_la_grammaire_de_rfc_6750():
    assert all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in entree_mcp.DESCRIPTION)


# --- émis après : servi, une seule lecture par appel -----------------------------

@pytest.mark.parametrize("jeton", [_JWT_APRES, _OTO_APRES], ids=["jwt", "oto_"])
def test_emis_apres_passe_avec_une_lecture_par_appel(lectures, jeton):
    with TestClient(_app_minimale()) as c:
        ouverture = _post(c, jeton)
        assert ouverture.status_code == 200, ouverture.text
        assert lectures == [_SUB], "initialize : une lecture, la garde n'en refait pas"
        sid = {"mcp-session-id": ouverture.headers["mcp-session-id"],
               "mcp-protocol-version": "2025-06-18"}
        _post(c, jeton, {"jsonrpc": "2.0", "method": "notifications/initialized"}, **sid)
        del lectures[:]
        appel = _post(c, jeton, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "ping", "arguments": {}}}, **sid)
    assert appel.status_code == 200, appel.text
    assert "pong" in appel.text
    assert lectures == [_SUB], "tools/call : une lecture, la garde n'en refait pas"


def test_la_garde_relit_quand_le_sub_canonique_differe(lectures, monkeypatch):
    """Ancien identifiant redirigé par le drain d'alias : l'état lu ne vaut pas pour le
    sub canonique, la garde lit le sien."""
    from oto_mcp.auth import hooks
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: "canonique")
    from oto_mcp.middleware import account_suspended
    monkeypatch.setattr(account_suspended, "current_user_sub_from_token",
                        lambda: "canonique")
    with TestClient(_app_minimale()) as c:
        assert _post(c, _JWT_APRES).status_code == 200
    assert lectures == [_SUB, "canonique"]


def test_un_get_de_flux_ne_paie_pas_la_lecture(lectures):
    """`GET /mcp` (flux de notifications) : pas de message JSON-RPC, pas de garde —
    et donc pas de lecture, comme avant."""
    with TestClient(_app_minimale()) as c:
        c.get("/mcp", headers={"host": "mcp.oto.cx", "accept": "text/event-stream",
                               "authorization": f"Bearer {_JWT_APRES}"})
    assert lectures == []

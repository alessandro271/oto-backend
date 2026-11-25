"""oto-backend#867 lot 2 — l'échange OAuth Google ne bloque pas la boucle
d'événements.

Le callback Google (route Starlette `async def`) faisait `exchange_code` puis
`persist_token` (→ `_fetch_email`) — deux appels HTTP synchrones. Depuis le
22/11/2026 le callback n'échange PLUS rien : l'échange vit dans la
complétion `me.federation.google.complete`, qui tourne dans le threadpool de
l'adaptateur REST comme toute capacité. Ce banc garde les deux moitiés : le callback
ne touche à aucun appel bloquant, et l'échange de la complétion part hors boucle.
"""
from __future__ import annotations

import asyncio
import threading

from starlette.requests import Request

from oto_mcp.api import oauth_returns
from oto_mcp.auth import google as google_oauth


def _callback_endpoint():
    return next(r.endpoint for r in oauth_returns.make_routes()
                if r.path == "/api/google/oauth/callback")


def _get(query: str) -> Request:
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "path": "/api/google/oauth/callback",
                    "headers": [], "query_string": query.encode(), "path_params": {}},
                   receive=_receive)


def _joue(coro):
    porteur: dict = {}

    async def _run():
        porteur["boucle"] = threading.current_thread()
        return await coro
    result = asyncio.run(_run())
    return porteur["boucle"], result


def test_le_callback_n_echange_rien(monkeypatch):
    def _jamais(*a, **k):
        raise AssertionError("le callback ne doit plus échanger")

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    monkeypatch.setattr(google_oauth, "exchange_code", _jamais)
    monkeypatch.setattr(google_oauth, "persist_token", _jamais)
    etat = google_oauth.make_state("sub-1", 42)
    _, result = _joue(_callback_endpoint()(_get(f"code=c&state={etat}")))
    assert "connect=confirm" in result.headers["location"]


def test_l_echange_de_la_completion_tourne_hors_boucle(monkeypatch):
    from oto_mcp import db
    from _datastore_rest import call, stub_authz

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    stub_authz(monkeypatch)
    monkeypatch.setattr(db, "consume_state_jti", lambda aud, jti: True)
    vu: dict = {}

    def _exchange(code, sub):
        vu["thread"] = threading.current_thread()
        return {"access_token": "a", "refresh_token": "r", "scope": "openid"}

    monkeypatch.setattr(google_oauth, "exchange_code", _exchange)
    monkeypatch.setattr(google_oauth, "persist_token", lambda *a, **k: "e@x.io")
    boucle = threading.current_thread()   # `call` joue la route dans CE thread
    code, _ = call("me.federation.google.complete",
                   body={"code": "c", "state": google_oauth.make_state("u-1", 35)})
    assert code == 200
    assert vu["thread"] is not boucle, (
        "exchange_code a tourné dans le thread de l'event loop — un Google lent "
        "gèlerait le processus entier (oto-backend#867)")


def test_le_controle_mord__un_appel_NU_dans_la_boucle_est_detecte():
    vu: dict = {}

    async def _nu():
        vu["thread"] = threading.current_thread()
        return 1

    boucle, _ = _joue(_nu())
    assert vu["thread"] is boucle, "la sonde elle-même doit savoir dire « dans la boucle »"

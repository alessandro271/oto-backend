"""Un 500 non géré sous /api/* porte son CORS — sinon le navigateur n'en lit rien.

Sans en-tête `Access-Control-Allow-Origin`, `fetch` rejette la réponse et le front
affiche « Failed to fetch » : impossible de distinguer une panne serveur d'une coupure
réseau (01/10/2026, une sonde en 500 après une pose réussie). Le middleware rend un
corps minimal, sans détail, puis relève l'exception pour qu'elle reste journalisée.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp import config
from oto_mcp.api.erreur_interne import ErreurInterneAvecCors

ORIGINE = "https://manage.oto.cx"


async def _boum(request):
    raise RuntimeError("secret interne — ne doit pas sortir")


async def _ok(request):
    return JSONResponse({"ok": True})


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "cors_origins", lambda: [ORIGINE])
    app = Starlette(routes=[Route("/api/boum", _boum), Route("/api/ok", _ok),
                            Route("/hors-api", _boum)],
                    middleware=[Middleware(ErreurInterneAvecCors)])
    return TestClient(app, raise_server_exceptions=False)


def test_un_500_sous_api_est_du_json_avec_cors(client):
    r = client.get("/api/boum", headers={"Origin": ORIGINE})
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error"}
    assert r.headers["access-control-allow-origin"] == ORIGINE
    assert "secret interne" not in r.text


def test_lexception_est_relevee_pour_rester_journalisee(monkeypatch):
    monkeypatch.setattr(config, "cors_origins", lambda: [ORIGINE])
    app = Starlette(routes=[Route("/api/boum", _boum)],
                    middleware=[Middleware(ErreurInterneAvecCors)])
    with pytest.raises(RuntimeError):
        TestClient(app).get("/api/boum", headers={"Origin": ORIGINE})


def test_une_origine_non_declaree_na_pas_de_cors(client):
    r = client.get("/api/boum", headers={"Origin": "https://ailleurs.example"})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_hors_api_rien_ne_change(client):
    r = client.get("/hors-api", headers={"Origin": ORIGINE})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_une_reponse_normale_passe_intacte(client):
    r = client.get("/api/ok", headers={"Origin": ORIGINE})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_le_serveur_monte_le_middleware():
    import inspect
    from oto_mcp import server
    assert "ErreurInterneAvecCors" in inspect.getsource(server)

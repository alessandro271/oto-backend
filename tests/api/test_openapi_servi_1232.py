"""`/openapi.json` rendu une fois, servi de la mémoire (oto-backend#1232).

Route anonyme, ~1,2 Mo, des centaines de millisecondes de construction SUR la boucle à
chaque appel : le moyen le moins cher de charger le processus. On ne la ferme pas (le
front d'un tenant et le dashboard y lisent leur contrat) ; on la rend une fois, hors de
la boucle, et on la sert avec un `ETag` fort et le 304.

Ce banc tient ce qui ne doit PAS bouger — le corps, octet pour octet celui que `_json`
sérialisait — et ce qui change : une construction par application, jamais sur la boucle,
l'`ETag`, le 304, le `Cache-Control`.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp import openapi
from oto_mcp.api import base, public

_HOTE = "https://mcp.exemple.test"


def _app(*routes) -> Starlette:
    return Starlette(routes=[
        Route("/openapi.json", public.openapi_doc, methods=["GET"]),
        Route("/api/openapi.json", public.openapi_doc, methods=["GET"]),
        *routes,
    ])


def test_le_corps_servi_est_celui_d_avant_octet_pour_octet():
    """Le contrôle de contrat d'un consommateur compare des octets : le document servi
    de la mémoire doit être exactement celui que `_json(openapi.build(...))` rendait,
    `servers` compris, sur la table de routes RÉELLE (alias dépréciés et leurs
    `operationId` compris)."""
    from oto_mcp import server
    app = Starlette(routes=server.routes_rest(object()))
    for hote in (_HOTE, "http://autre.exemple.test:8080"):
        r = TestClient(app, base_url=hote).get("/api/openapi.json")
        assert r.status_code == 200
        attendu = JSONResponse(openapi.build(app.routes, server_url=hote)).body
        assert r.content == attendu
        assert r.headers["content-type"] == "application/json"


def test_construit_une_fois_par_application_et_jamais_sur_la_boucle(monkeypatch):
    appels = []
    vrai_build = openapi.build

    def build_compte(*a, **k):
        # Dans le threadpool, aucune boucle ne tourne : sur la boucle, elle tournerait.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        appels.append(k)
        return vrai_build(*a, **k)

    monkeypatch.setattr(openapi, "build", build_compte)
    app = _app()
    un = TestClient(app, base_url=_HOTE)
    deux = TestClient(app, base_url="https://autre.exemple.test")
    corps = [un.get("/openapi.json"), un.get("/api/openapi.json"), deux.get("/openapi.json")]
    assert len(appels) == 1, "un seul rendu par application, quel que soit l'hôte"
    assert "server_url" not in appels[0], "le rendu mis en mémoire ne dépend d'aucune requête"
    assert corps[0].json()["servers"] == [{"url": _HOTE}]
    assert corps[2].json()["servers"] == [{"url": "https://autre.exemple.test"}]
    assert corps[0].headers["etag"] != corps[2].headers["etag"], (
        "deux corps différents, deux empreintes")

    _app_neuve = _app()
    TestClient(_app_neuve).get("/openapi.json")
    assert len(appels) == 2, "une autre application (une autre table de routes) a son rendu"


def test_etag_fort_cache_control_et_304():
    client = TestClient(_app(), base_url=_HOTE)
    r = client.get("/openapi.json", headers={"Origin": "https://inconnue.exemple.test"})
    etag = r.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/"), "un ETag FORT"
    assert r.headers["cache-control"] == "public, max-age=300"
    assert r.headers["vary"] == "Origin"
    assert client.get("/openapi.json").headers["etag"] == etag, "stable d'un appel à l'autre"

    for valeur in (etag, f"W/{etag}", f'"autre", {etag}', "*"):
        r304 = client.get("/openapi.json", headers={"If-None-Match": valeur})
        assert r304.status_code == 304, valeur
        assert r304.content == b""
        assert r304.headers["etag"] == etag
        assert r304.headers["cache-control"] == "public, max-age=300"

    assert client.get("/openapi.json", headers={"If-None-Match": '"perime"'}).status_code == 200


def test_le_304_porte_son_cors(monkeypatch):
    """Le dashboard revalide depuis le navigateur : un 304 sans CORS y serait illisible."""
    monkeypatch.setattr(base, "_allowed_origins", lambda: ["https://console.exemple.test"])
    client = TestClient(_app(), base_url=_HOTE)
    origine = {"Origin": "https://console.exemple.test"}
    etag = client.get("/openapi.json", headers=origine).headers["etag"]
    r = client.get("/openapi.json", headers={**origine, "If-None-Match": etag})
    assert r.status_code == 304
    assert r.headers["access-control-allow-origin"] == "https://console.exemple.test"

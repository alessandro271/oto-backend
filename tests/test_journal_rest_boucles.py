"""Le journal REST n'écrit pas les boucles de machines (`api.routes.NON_JOURNALISES`).

Les réclamations de travail des workers, les relevés d'usage d'un service et la
sonde de version remplissaient le journal d'appels aux deux tiers, sans qu'aucune
lecture n'en dépende. Tenu ici, sur les VRAIES capacités montées par l'adaptateur
REST (handler doublé, sans base) et enveloppées du vrai middleware :

  1. une réclamation qui n'attribue rien ne s'écrit pas ; celle qui attribue un job,
     ou qui dit pourquoi elle n'a pas pu regarder, s'écrit ;
  2. un autre geste de la même route s'écrit, même s'il ne rend rien ;
  3. un tour ajouté au fil d'un run ne s'écrit pas ; la lecture du fil, si ;
  4. une réponse en erreur s'écrit toujours ;
  5. chaque entrée des listes désigne une route servie.
"""
from __future__ import annotations

import asyncio
import dataclasses
import re
import typing

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from oto_mcp.api import base as api_base
from oto_mcp.api import routes as ar
from oto_mcp.capabilities import run_thread, runner_jobs  # noqa: F401 — enregistrent
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES


def _capacite(key: str):
    (c,) = [c for c in CAPABILITIES if c.key == key]
    return c


@pytest.fixture
def journal(monkeypatch):
    lignes: list[dict] = []

    def _emet(row):
        lignes.append(dict(row))

        async def _fait():
            return None
        return _fait()

    monkeypatch.setattr(ar, "_emit_rest_event", _emet)
    return lignes


def _client(key: str, reponse):
    """La route de la capacité `key`, handler doublé : `reponse(inp)` est ce qu'il rend."""
    from oto_mcp.capabilities import _rest_adapter

    async def authentifie(request, verifier, **kw):
        return "u-boucle", None

    c = dataclasses.replace(
        _capacite(key),
        authz=lambda raw, inp: ResolvedCtx(sub=raw.sub, org_id=None),
        handler=lambda ctx, inp: reponse(inp))
    routes = _rest_adapter.make_routes(None, authentifie, api_base._json,
                                       api_base._json_error, api_base.options_handler, [c])
    return TestClient(Starlette(routes=routes, middleware=[Middleware(ar.RestCallLogger)]))


def test_une_reclamation_vide_ne_s_ecrit_pas(journal):
    r = _client("runner.jobs", lambda inp: {"job": None}).post(
        "/api/me/runner/jobs", json={"op": "claim"})
    assert r.status_code == 200, r.text
    assert r.json() == {"job": None}
    assert journal == []


@pytest.mark.parametrize("rendu", [{"job": {"id": 7}},
                                   {"job": None, "campaign_error": "panne"}])
def test_une_reclamation_qui_rend_quelque_chose_s_ecrit(journal, rendu):
    _client("runner.jobs", lambda inp: rendu).post("/api/me/runner/jobs",
                                                   json={"op": "claim"})
    assert [l["tool"] for l in journal] == ["POST /api/me/runner/jobs"]


def test_un_autre_geste_de_la_route_s_ecrit_meme_vide(journal):
    _client("runner.jobs", lambda inp: {"job": None}).post(
        "/api/me/runner/jobs", json={"op": "get", "job_id": 1})
    assert [l["tool"] for l in journal] == ["POST /api/me/runner/jobs"]


def test_un_tour_ajoute_au_fil_ne_s_ecrit_pas_sa_lecture_si(journal):
    client = _client("runs.thread", lambda inp: {"run_id": "r-1", "turns": []})
    client.post("/api/me/runs/thread", json={"op": "append", "run_id": "r-1",
                                             "role": "assistant", "content": {"text": "x"}})
    assert journal == []
    client.post("/api/me/runs/thread", json={"op": "read", "run_id": "r-1"})
    assert [l["tool"] for l in journal] == ["POST /api/me/runs/thread"]


def _mw(path: str, method: str, status: int, journal) -> None:
    async def aval(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def recoit():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def envoie(m):
        return None

    async def drive():
        await ar.RestCallLogger(aval)({"type": "http", "path": path, "method": method,
                                       "headers": [], "query_string": b""},
                                      recoit, envoie)
        await asyncio.sleep(0)

    asyncio.run(drive())


@pytest.mark.parametrize("path", ["/api/version", "/api/orgs/7/usage/tools",
                                  "/api/orgs/7/usage/connections",
                                  "/api/orgs/7/usage/calls"])
def test_une_lecture_de_service_ne_s_ecrit_pas_sauf_en_erreur(journal, path):
    _mw(path, "GET", 200, journal)
    assert journal == []
    _mw(path, "GET", 429, journal)
    assert [l["error"] for l in journal] == ["HTTP 429"]


def test_chaque_entree_designe_une_route_servie():
    """Une entrée qui ne désigne plus rien (route renommée) laisserait la boucle
    revenir au journal sans que personne ne le voie."""
    servies = set()
    for route in ar.make_routes(object(), mcp_instance=None):
        gabarit = re.sub(r"\{[^}]+\}", ":id", route.path)
        for methode in route.methods or ():
            servies.add((methode, gabarit))
    for methode, route, op in ar.NON_JOURNALISES | ar.NON_JOURNALISES_SI_VIDES:
        assert (methode, route) in servies, (methode, route)
        if op is not None:
            (c,) = [c for c in CAPABILITIES
                    if any((b.verb, b.path) == (methode, route) for b in c.rest_bindings())]
            assert op in typing.get_args(c.Input.model_fields["op"].annotation), op

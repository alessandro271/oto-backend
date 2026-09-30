"""Sentry nomme la transaction sans refaire le routage (oto-backend#1108).

Le style par défaut de l'intégration Starlette (`url`) rebalaie, dans la boucle et à
chaque requête REST, toute la table de routes pour retrouver un gabarit que Starlette
vient de résoudre (`_transaction_name_from_router`, ~8 % du thread principal au relevé
py-spy de prod). `sentry_setup` pose `transaction_style="endpoint"`, qui lit
l'endpoint déjà résolu. Preuves :

1. `test_init_pose_le_style_endpoint` — la configuration elle-même.
2. `test_chaque_route_de_capacite_porte_son_propre_nom` — le style `endpoint` nomme
   d'après la fonction : les routes de capacité, toutes fabriquées par la même
   fermeture, portent chacune « VERBE chemin » au lieu d'un nom commun.
3. `test_une_requete_ne_refait_plus_le_routage` — sur la VRAIE table de routes et un
   VRAI événement d'erreur (transport collecteur, aucun réseau), dans un processus à
   part : l'intégration Starlette se patche pour tout le processus, elle ne doit pas
   coller au reste de la suite. Zéro balayage par requête, et l'événement porte le
   nom de la route de capacité.
4. `test_une_erreur_d_outil_porte_le_nom_de_son_outil` — `/mcp` n'est pas une route
   nommable par endpoint : sans rien de plus, son erreur porterait le nom du dernier
   middleware Starlette traversé. Le middleware d'outil la nomme `mcp:<outil>`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest
import sentry_sdk
from fastmcp import Client, FastMCP
from sentry_sdk.integrations.mcp import MCPIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.utils import transaction_from_function

from oto_mcp import sentry_setup


def test_init_pose_le_style_endpoint(monkeypatch):
    vus: dict = {}
    monkeypatch.setattr(sentry_setup.sentry_sdk, "init", lambda **kw: vus.update(kw))
    monkeypatch.setattr(sentry_setup, "ignore_logger", lambda name: None)
    monkeypatch.setenv("OTO_SENTRY_DSN", "https://x@example.invalid/1")

    assert sentry_setup.init_sentry() is True

    starlette = [i for i in vus.get("integrations", []) if isinstance(i, StarletteIntegration)]
    assert len(starlette) == 1 and starlette[0].transaction_style == "endpoint", (
        "le style `url` (défaut du SDK) refait le routage de toute la table à chaque "
        "requête REST, dans la boucle")


def test_chaque_route_de_capacite_porte_son_propre_nom():
    from oto_mcp.api import routes as api_routes

    class _Verifier:
        """Capturé au montage, jamais appelé."""

    capacites = [r for r in api_routes.make_routes(_Verifier(), mcp_instance=None)
                 if r.endpoint.__module__ == "oto_mcp.capabilities._rest_adapter"]
    assert len(capacites) > 100, "la table devrait porter les routes de capacité"
    noms = [transaction_from_function(r.endpoint) for r in capacites]
    assert len(set(noms)) == len(noms), "deux routes de capacité partagent un nom"
    for route, nom in zip(capacites, noms):
        (verbe,) = route.methods - {"HEAD"}
        assert nom == f"oto_mcp.capabilities._rest_adapter.{verbe} {route.path}"
        # `route.name` (donc la table de routes figée) ne bouge pas.
        assert route.name == "_handler"


_SOUS_PROCESSUS = textwrap.dedent("""
    import json, os, re, sys
    import sentry_sdk
    from sentry_sdk.transport import Transport

    evenements = []

    class Collecteur(Transport):
        def capture_envelope(self, envelope):
            ev = envelope.get_event()
            if ev is not None:
                evenements.append(ev)

    _init = sentry_sdk.init
    sentry_sdk.init = lambda **kw: _init(transport=Collecteur, **kw)

    from oto_mcp import sentry_setup
    assert sentry_setup.init_sentry()

    from sentry_sdk.integrations import starlette as integ
    balayages = []
    _balaye = integ._transaction_name_from_router
    def espion(scope):
        balayages.append(scope.get("path"))
        return _balaye(scope)
    integ._transaction_name_from_router = espion

    from oto_mcp.api import routes as api_routes

    async def panne(request, verifier, **kw):
        raise RuntimeError("panne provoquée")
    api_routes._authenticate = panne

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    class Verifier: pass
    app = Starlette(routes=api_routes.make_routes(Verifier(), mcp_instance=None))
    route = next(r for r in app.routes
                 if getattr(r.endpoint, "__qualname__", "").startswith("GET /api/"))
    client = TestClient(app, raise_server_exceptions=False)
    statut = client.get(re.sub(r"\\{[^}]+\\}", "1", route.path)).status_code
    derniere = app.routes[-1]
    client.options(re.sub(r"\\{[^}]+\\}", "1", derniere.path))
    sentry_sdk.flush()
    print(json.dumps({"statut": statut, "route": route.path, "balayages": balayages,
                      "transactions": [e.get("transaction") for e in evenements]}))
""")


def test_une_requete_ne_refait_plus_le_routage():
    env = dict(os.environ, OTO_SENTRY_DSN="https://x@o0.ingest.sentry.io/0",
               OTO_MCP_CORS_ORIGINS="http://exemple.invalid")
    env.pop("OTO_SENTRY_TRACES_SAMPLE_RATE", None)
    sortie = subprocess.run([sys.executable, "-c", _SOUS_PROCESSUS], env=env,
                            capture_output=True, text=True, timeout=300)
    assert sortie.returncode == 0, sortie.stderr[-3000:]
    res = json.loads(sortie.stdout.strip().splitlines()[-1])

    assert res["statut"] == 500, res
    assert res["balayages"] == [], (
        f"le SDK a rebalayé la table de routes : {res['balayages']}")
    assert res["transactions"] == [
        f"oto_mcp.capabilities._rest_adapter.GET {res['route']}"], res


class _Collecteur(Transport):
    def __init__(self, options=None) -> None:
        super().__init__(options)
        self.evenements: list = []

    def capture_envelope(self, envelope) -> None:
        ev = envelope.get_event()
        if ev is not None:
            self.evenements.append(ev)


@pytest.mark.asyncio
async def test_une_erreur_d_outil_porte_le_nom_de_son_outil(monkeypatch):
    monkeypatch.setattr(sentry_setup, "current_user_sub_from_token", lambda: None)
    monkeypatch.setattr(sentry_setup, "current_client_id_from_token", lambda: None)
    precedent = sentry_sdk.get_client()
    collecteur = _Collecteur()
    sentry_sdk.init(
        dsn="https://x@o0.ingest.sentry.io/0", transport=collecteur,
        include_local_variables=False, traces_sample_rate=0,
        before_send=sentry_setup._before_send,
        disabled_integrations=[MCPIntegration()],
        default_integrations=False,  # rien qui colle au processus après ce test
    )
    mcp = FastMCP("t")

    @mcp.tool()
    def boom() -> str:
        raise RuntimeError("panne réelle, pas une erreur gérée")

    mcp.add_middleware(sentry_setup.SentryToolErrorMiddleware())
    try:
        async with Client(mcp) as c:
            with pytest.raises(Exception):
                await c.call_tool("boom", {})
    finally:
        sentry_sdk.get_global_scope().set_client(precedent)

    assert [e.get("transaction") for e in collecteur.evenements] == ["mcp:boom"]
    assert collecteur.evenements[0]["transaction_info"] == {"source": "custom"}

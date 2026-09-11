"""Le compteur des refus du transport MCP : compte-t-il, et SÉPARE-T-IL les causes ?

**Les refus sont FABRIQUÉS contre un vrai transport `mcp`, pas contre des corps de
réponse écrits à la main.** C'est la seule forme qui vaille : la cause ne se lit que
dans le message du SDK, donc un banc qui se fournirait à lui-même ses propres corps
prouverait que le classifieur sait lire ce que le banc a écrit — rien d'autre. Ici,
si l'amont reformule un message, le cas tombe dans `autre` et ce fichier rougit.

Ce qu'il tient :

- les CAUSES sont séparées — un compteur global dirait ce que le journal d'accès dit
  déjà, c'est-à-dire rien ;
- le chemin NOMINAL n'est jamais compté, et rien n'y est bufferisé ;
- aucune donnée du client n'est enregistrée. Le corps d'une erreur de validation
  porte le détail pydantic, donc le payload : un canari le prouve ;
- ce qui n'est pas le endpoint MCP traverse sans être touché ;
- une exception traverse le compteur intacte (la garde de déconnexion, posée
  au-dessus, doit continuer à la voir).
"""
from __future__ import annotations

import asyncio
import json

import anyio
import httpx
import pytest
from fastmcp import FastMCP

from oto_mcp import transport_refusals
from oto_mcp.transport_refusals import TransportRefusalCounter, cause_du_refus

ACCEPT = "application/json, text/event-stream"
INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "banc", "version": "0"}}})


def _app():
    srv = FastMCP("banc-refus")

    @srv.tool
    def ping(x: str = "") -> str:
        return f"pong {x}"

    return srv.http_app()


async def _drainer():
    """Le comptage part en tâche de fond : l'attendre avant d'affirmer quoi que ce soit.

    Sans ça le banc lirait une liste vide et conclurait « rien n'est compté » — un zéro
    crédible et faux, qui viendrait du banc et non du code."""
    for _ in range(50):
        en_vol = list(transport_refusals._EN_VOL)
        if not en_vol:
            return
        await asyncio.gather(*en_vol, return_exceptions=True)
    raise AssertionError("des comptages sont restés en vol")


async def _jouer(fabrique, _inutile=None):
    """Ouvre une VRAIE session, puis joue les requêtes que `fabrique` en dérive.

    La session compte : plusieurs refus n'arrivent qu'à une requête qui en porte une
    (le transport valide la session AVANT la version de protocole, donc sans session
    on mesure toujours la même cause en croyant en mesurer une autre — erreur faite
    en écrivant ce banc)."""
    app = TransportRefusalCounter(_app())
    interne = app.app
    async with interne.router.lifespan_context(interne):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://banc.test") as client:
            ouverture = await client.post(
                "/mcp", headers={"Content-Type": "application/json", "Accept": ACCEPT},
                content=INIT)
            assert ouverture.status_code == 200, ouverture.text
            sid = ouverture.headers["mcp-session-id"]
            reponses = []
            for entetes, corps, chemin in fabrique(sid):
                reponses.append(await client.post(chemin, headers=entetes, content=corps))
            await _drainer()
            return reponses


@pytest.fixture
def lignes(monkeypatch):
    """Intercepte l'ÉCRITURE en base, pas le classement : `cause_du_refus` et la
    composition de la ligne tournent pour de vrai."""
    ecrites: list[dict] = []
    monkeypatch.setattr(transport_refusals, "_ecrire", ecrites.append)
    monkeypatch.setenv("OTO_SENTRY_ENV", "canari")
    return ecrites


def _causes(lignes) -> list[str]:
    return [l["tool"] for l in lignes]


# --- ce qui doit être COMPTÉ, et SÉPARÉ --------------------------------------

def test_les_causes_de_refus_sont_comptees_et_distinguees(lignes):
    """Les quatre causes annoncées — plus celle qu'on a trouvée en mesurant, le
    `Content-Type` refusé, que le transport rend AVANT tout le reste et en texte brut
    (pas en JSON-RPC) : elle ne se classe pas comme les autres."""
    json_h = {"Content-Type": "application/json", "Accept": ACCEPT}
    tools_list = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                             "params": {}})

    def fabrique(sid):
        return [
            (json_h, "{ ceci n'est pas du json", "/mcp"),
            (json_h, '{"bonjour": 1}', "/mcp"),
            (json_h, tools_list, "/mcp"),
            ({**json_h, "Mcp-Session-Id": sid, "MCP-Protocol-Version": "2019-01-01"},
             tools_list, "/mcp"),
            ({"Content-Type": "text/plain", "Accept": ACCEPT}, INIT, "/mcp"),
            ({**json_h, "Mcp-Session-Id": "0" * 32}, tools_list, "/mcp"),
        ]

    reponses = anyio.run(_jouer, fabrique)

    assert [r.status_code for r in reponses] == [400, 400, 400, 400, 400, 404]
    # Comparaison par ENSEMBLE, pas par ordre : les écritures partent en tâche de fond
    # et se terminent dans l'ordre qu'elles veulent. Asserter l'ordre ferait un banc
    # qui rougit au hasard, et qu'on finirait par rejouer jusqu'à ce qu'il passe.
    assert sorted(_causes(lignes)) == sorted([
        "refus:json_illisible",
        "refus:message_non_conforme",
        "refus:session_absente",
        "refus:version_refusee",
        "refus:content_type_refuse",
        "refus:session_inconnue",
    ])
    # Cinq refus sur six portent le MÊME code HTTP : c'est exactement pourquoi un
    # compteur qui ne lirait que le statut ne servirait à rien.
    assert len({l["args"]["statut"] for l in lignes}) == 2
    assert len(set(_causes(lignes))) == 6
    assert all(l["kind"] == "transport" and l["ok"] is False for l in lignes)
    assert all(l["args"]["env"] == "canari" for l in lignes)


def test_la_version_refusee_est_la_seule_donnee_variable_enregistree(lignes):
    """Savoir QUELLE version un client réclame est le seul détail actionnable — c'est
    ce qui dira, le jour venu, qu'un logiciel est écarté. Il n'est retenu que pour ce
    cas et que s'il a la forme d'une date."""
    anyio.run(_jouer, lambda sid: [
        ({"Content-Type": "application/json", "Accept": ACCEPT,
          "Mcp-Session-Id": sid, "MCP-Protocol-Version": "2019-01-01"},
         json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
         "/mcp"),
    ])
    assert lignes[0]["args"]["version_demandee"] == "2019-01-01"


def test_une_cause_inconnue_ne_fabrique_pas_d_etiquette():
    """Le vocabulaire est FERMÉ : si l'amont reformule, on le voit (`autre`) au lieu
    de voir le compteur se mettre à suivre les phrases du SDK."""
    assert cause_du_refus(b'{"error":{"message":"Quelque chose de neuf"}}') == ("autre", None)
    assert cause_du_refus(b"") == ("autre", None)


# --- ce qui ne doit RIEN coûter, et RIEN enregistrer -------------------------

def test_le_chemin_nominal_n_est_jamais_compte(lignes):
    """Deux appels qui réussissent : aucune ligne, et aucun corps accumulé."""
    json_h = {"Content-Type": "application/json", "Accept": ACCEPT}

    async def scenario():
        app = TransportRefusalCounter(_app())
        interne = app.app
        async with interne.router.lifespan_context(interne):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://banc.test") as client:
                r1 = await client.post("/mcp", headers=json_h, content=INIT)
                sid = r1.headers.get("mcp-session-id")
                r2 = await client.post("/mcp", headers={
                    **json_h, "Mcp-Session-Id": sid,
                    "MCP-Protocol-Version": "2025-06-18"},
                    content=json.dumps({"jsonrpc": "2.0", "id": 2,
                                        "method": "tools/list", "params": {}}))
                await _drainer()
                return r1.status_code, r2.status_code

    assert anyio.run(scenario) == (200, 200)
    assert lignes == []


def test_hors_du_endpoint_mcp_le_compteur_est_transparent(lignes):
    """Un 404 ailleurs n'est pas un refus de transport. Et l'égalité de chemin est
    EXACTE : le PRM est servi sur `…/oauth-protected-resource/mcp`, qui se termine
    aussi par `/mcp` — un `endswith` le compterait."""
    vus: list[str] = []

    async def app_nue(scope, receive, send):
        vus.append(scope["path"])
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"introuvable"})

    async def scenario():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=TransportRefusalCounter(app_nue)),
                base_url="http://banc.test") as client:
            for chemin in ("/api/me", "/.well-known/oauth-protected-resource/mcp"):
                await client.get(chemin)
            await _drainer()

    anyio.run(scenario)
    assert vus == ["/api/me", "/.well-known/oauth-protected-resource/mcp"]
    assert lignes == []


# --- ce qui ne doit JAMAIS être enregistré -----------------------------------

def test_aucune_donnee_du_client_n_atteint_la_ligne(lignes):
    """Le corps d'une erreur de validation porte le détail pydantic, donc le payload
    du client. On n'en garde qu'une étiquette : un canari envoyé dans la requête ne
    doit se retrouver NULLE PART dans ce qu'on écrit."""
    # Court exprès : pydantic TRONQUE le milieu des valeurs longues qu'il cite, donc
    # un canari long ne survivrait pas au corps servi et le contrôle ci-dessous
    # passerait sans rien prouver.
    canari = "CANARI42"
    reponses = anyio.run(_jouer, lambda sid: [
        ({"Content-Type": "application/json", "Accept": ACCEPT},
         json.dumps({"jsonrpc": "2.0", "id": 1, "method": 12345,
                     "params": {"jeton": canari}}), "/mcp"),
    ])

    # Contrôle de l'instrument : le canari est bien DANS la réponse servie, sinon ce
    # test passerait pour une raison sans rapport avec ce qu'il prétend prouver.
    assert reponses[0].status_code == 400
    assert canari in reponses[0].text
    assert _causes(lignes) == ["refus:message_non_conforme"]
    assert canari not in json.dumps(lignes[0])
    assert lignes[0].get("sub") is None        # pas d'identité à cette couche
    assert set(lignes[0]["args"]) <= {"statut", "methode", "env", "version_demandee"}


# --- ce qui doit TRAVERSER ---------------------------------------------------

def test_une_exception_traverse_le_compteur(lignes):
    """La garde de déconnexion est posée AU-DESSUS : si le compteur avalait une
    exception, elle ne la verrait plus et la connexion resterait empoisonnée."""

    async def app_qui_leve(scope, receive, send):
        raise RuntimeError("boum")

    async def scenario():
        compteur = TransportRefusalCounter(app_qui_leve)
        with pytest.raises(RuntimeError, match="boum"):
            await compteur({"type": "http", "method": "POST", "path": "/mcp",
                            "headers": []}, None, None)

    anyio.run(scenario)
    assert lignes == []


def test_un_echec_d_ecriture_ne_casse_jamais_la_reponse(monkeypatch):
    """Le compteur est best-effort : la base peut être indisponible, la requête
    refusée doit quand même recevoir sa réponse complète."""
    def _tombe(_ligne):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(transport_refusals, "_ecrire", _tombe)
    reponses = anyio.run(_jouer, lambda sid: [
        ({"Content-Type": "application/json", "Accept": ACCEPT}, "{ pas du json", "/mcp"),
    ])
    assert reponses[0].status_code == 400


# --- l'assemblage : le compteur est bien POSÉ, et à la bonne place -----------

def test_le_compteur_est_pose_dans_lapp_racine_servie_par_uvicorn():
    """Un instrument qu'on peut retirer sans faire rougir la suite ne mesure rien.

    SOUS la garde de déconnexion (qui synthétise une réponse quand le client est
    parti — ce n'est pas un refus, il ne faut pas la compter) et AU-DESSUS du
    dispatch par Host, donc il couvre l'instance canonique ET l'instance anonyme
    des sous-domaines de projet."""
    from oto_mcp.client_disconnect_guard import ClientDisconnectGuard
    from oto_mcp.server import build_root_app
    from oto_mcp.subdomain_project import HostDispatch

    racine = build_root_app(object(), object())
    assert isinstance(racine, ClientDisconnectGuard)
    assert isinstance(racine.app, TransportRefusalCounter)
    # …et le dispatch est bien SOUS lui.
    couche = racine.app.app
    while not isinstance(couche, HostDispatch):
        couche = couche.app
    assert isinstance(couche, HostDispatch)

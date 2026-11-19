"""`sessions_mcp` contre le VRAI SDK `mcp` : une vraie face fastmcp, de vraies sessions
Streamable HTTP, le vrai routeur du transport.

Chaque correctif a son TÉMOIN, qui joue la même scène sans l'enveloppe et constate le
défaut : le jour où l'SDK le corrige lui-même, le témoin rougit et l'enveloppe est à
retirer ; le jour où l'SDK déplace ce sur quoi elle s'accroche, le banc équipé rougit.
La libération se prouve par une référence faible, jamais par une mesure de mémoire."""
from __future__ import annotations

import contextlib
import gc
import logging
import types
import weakref

import anyio
import httpx
import pytest
from mcp.types import JSONRPCResponse

from oto_mcp import sessions_mcp

_ENTETES = {"accept": "application/json, text/event-stream",
            "content-type": "application/json"}
_GROS = 300_000


class _Espion:
    """Le flux d'écriture, entre l'enveloppe et le transport : retient une référence
    FAIBLE de chaque réponse qui le traverse (hors témoin)."""

    def __init__(self, flux, vues):
        self._flux, self._vues = flux, vues

    def __getattr__(self, nom):
        return getattr(self._flux, nom)

    async def __aenter__(self):
        await self._flux.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._flux.__aexit__(*exc)

    async def send(self, message):
        racine = message.message.root
        if isinstance(racine, JSONRPCResponse) and not str(racine.id).startswith("oto-"):
            self._vues.append(weakref.ref(message))
        await self._flux.send(message)


class _ServeurEspion:
    def __init__(self, serveur, vues):
        self._serveur, self._vues = serveur, vues

    def __getattr__(self, nom):
        return getattr(self._serveur, nom)

    async def run(self, read_stream, write_stream, *args, **kwargs):
        return await self._serveur.run(read_stream, _Espion(write_stream, self._vues),
                                       *args, **kwargs)


def _face():
    from fastmcp import FastMCP
    serveur = FastMCP("banc")

    @serveur.tool
    def gros() -> str:
        return "x" * _GROS

    return serveur.http_app(path="/mcp")


def _gestionnaire(face):
    from oto_mcp.borne_session import gestionnaires
    (g,) = gestionnaires(face)
    return g


@contextlib.asynccontextmanager
async def _banc(equiper: bool, idle_s: float | None = None):
    face = _face()
    vues: list = []
    async with face.router.lifespan_context(face):
        g = _gestionnaire(face)
        g.session_idle_timeout = idle_s
        # L'espion SOUS l'enveloppe : il voit ce qu'elle fait suivre au transport (et
        # l'enveloppe reçoit le vrai flux du transport, auquel elle le reconnaît).
        g.app = _ServeurEspion(g.app, vues)
        equipes = sessions_mcp.equiper([face], ["banc"]) if equiper else []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=face),
                                     base_url="http://localhost") as client:
            yield types.SimpleNamespace(client=client, g=g, vues=vues, equipes=equipes)


async def _ouvrir(b) -> dict:
    r = await b.client.post("/mcp", headers=_ENTETES, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "banc", "version": "0"}}})
    assert r.status_code == 200, r.text
    entetes = dict(_ENTETES, **{"mcp-session-id": r.headers["mcp-session-id"]})
    await b.client.post("/mcp", headers=entetes,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return entetes


async def _grosse_reponse(b, entetes) -> weakref.ref:
    r = await b.client.post("/mcp", headers=entetes, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "gros", "arguments": {}}})
    assert r.status_code == 200 and len(r.text) > _GROS
    return b.vues[-1]


async def _laisser_passer():
    for _ in range(10):
        await anyio.sleep(0.01)
    gc.collect()


# ── 1. la dernière réponse ───────────────────────────────────────────────────

def test_temoin_sans_enveloppe_le_routeur_retient_la_derniere_reponse():
    async def scene():
        async with _banc(equiper=False) as b:
            reponse = await _grosse_reponse(b, await _ouvrir(b))
            await _laisser_passer()
            vivante = reponse()
            assert vivante is not None, (
                "l'SDK ne retient plus la dernière réponse : retirer l'enveloppe "
                "`_EcritureLiberatrice` de `sessions_mcp`")
            retenteurs = [getattr(r, "cr_code", None) for r in gc.get_referrers(vivante)]
            assert any(c is not None and c.co_name == "message_router" for c in retenteurs)
    anyio.run(scene)


def test_equipee_la_reponse_n_est_plus_atteignable_session_ouverte():
    async def scene():
        async with _banc(equiper=True) as b:
            entetes = await _ouvrir(b)
            reponse = await _grosse_reponse(b, entetes)
            await _laisser_passer()
            assert reponse() is None, "la réponse servie reste atteignable"
            assert entetes["mcp-session-id"] in b.g._server_instances, "session ouverte"
            # La session sert toujours, et le témoin n'est livré à personne.
            r = await b.client.post("/mcp", headers=entetes, json={
                "jsonrpc": "2.0", "id": 3, "method": "ping"})
            assert r.status_code == 200 and '"id":3' in r.text and "oto-" not in r.text
    anyio.run(scene)


# ── 2. la session fermée par DELETE ──────────────────────────────────────────

async def _delete(b, entetes):
    sid = entetes["mcp-session-id"]
    transport = weakref.ref(b.g._server_instances[sid])
    r = await b.client.delete("/mcp", headers=entetes)
    assert r.status_code == 200
    await _laisser_passer()
    return sid, transport


def test_temoin_sans_enveloppe_une_session_delete_reste_au_gestionnaire():
    async def scene():
        async with _banc(equiper=False) as b:
            sid, _ = await _delete(b, await _ouvrir(b))
            assert sid in b.g._server_instances, (
                "l'SDK retire maintenant la session terminée : retirer ce retrait de "
                "`_ServeurLiberateur._fin_de_session`")
            assert b.g._server_instances[sid].is_terminated
    anyio.run(scene)


def test_equipee_une_session_delete_est_retiree_et_rendue():
    async def scene():
        async with _banc(equiper=True) as b:
            sid, transport = await _delete(b, await _ouvrir(b))
            assert sid not in b.g._server_instances
            assert sid not in b.g._session_owners
            assert transport() is None, "le transport terminé reste atteignable"
            (e,) = b.equipes
            assert (e.inactivite, e.delete) == (0, 1)
    anyio.run(scene)


def test_equipee_une_session_inactive_est_comptee_a_part():
    async def scene():
        async with _banc(equiper=True, idle_s=0.05) as b:
            entetes = await _ouvrir(b)
            await anyio.sleep(0.2)
            await _laisser_passer()
            assert entetes["mcp-session-id"] not in b.g._server_instances
            (e,) = b.equipes
            assert (e.inactivite, e.delete) == (1, 0)
    anyio.run(scene)


# ── la ligne de contrôle ─────────────────────────────────────────────────────

def test_la_ligne_compte_depuis_la_precedente():
    async def scene():
        async with _banc(equiper=True) as b:
            await _delete(b, await _ouvrir(b))
            await _ouvrir(b)
            assert sessions_mcp.ligne(b.equipes) == (
                "sessions MCP : banc ouvertes=1 terminees_retenues=0 "
                "fermees_inactivite=0 fermees_delete=1")
            assert sessions_mcp.ligne(b.equipes).endswith("fermees_delete=0")
    anyio.run(scene)


def test_la_veille_journalise_en_info(caplog):
    async def scene():
        async with _banc(equiper=True) as b:
            with caplog.at_level(logging.INFO, logger="oto_mcp.sessions_mcp"):
                with anyio.move_on_after(0.1):
                    await sessions_mcp.veiller(b.equipes, intervalle=0.03)
    anyio.run(scene)
    lignes = [r.getMessage() for r in caplog.records if r.name == "oto_mcp.sessions_mcp"]
    assert lignes and lignes[0] == (
        "sessions MCP : banc ouvertes=0 terminees_retenues=0 fermees_inactivite=0 "
        "fermees_delete=0 (sur 0 s)")


# ── les accroches ────────────────────────────────────────────────────────────

def test_equiper_est_idempotent():
    async def scene():
        face = _face()
        async with face.router.lifespan_context(face):
            premieres = sessions_mcp.equiper([face])
            assert sessions_mcp.equiper([face]) == premieres
            assert not isinstance(_gestionnaire(face).app._serveur,
                                  sessions_mcp._ServeurLiberateur)
    anyio.run(scene)


def _factice(**champs):
    base = dict(stateless=False, app=types.SimpleNamespace(run=lambda *a: None),
                _server_instances={}, _session_owners={}, event_store=None)
    return types.SimpleNamespace(session_manager=types.SimpleNamespace(**{**base, **champs}))


def test_un_gestionnaire_sans_ses_accroches_refuse():
    for manque in ("_server_instances", "_session_owners", "app"):
        face = _factice()
        delattr(face.session_manager, manque)
        with pytest.raises(sessions_mcp.AccrocheSdkAbsente):
            sessions_mcp.equiper([face])


def test_un_event_store_refuse_il_rejouerait_le_temoin():
    with pytest.raises(sessions_mcp.AccrocheSdkAbsente, match="event_store"):
        sessions_mcp.equiper([_factice(event_store=object())])


def test_une_face_sans_etat_n_est_pas_equipee():
    assert sessions_mcp.equiper([_factice(stateless=True)]) == []

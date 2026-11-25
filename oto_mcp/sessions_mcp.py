"""Hygiène mémoire des sessions MCP ouvertes, et sa ligne de contrôle.

Un défaut du transport Streamable HTTP de l'SDK `mcp` (relevé en 1.28.x, toujours là en
1.30.0), corrigé sans le forker, par une enveloppe posée au démarrage sur le serveur bas
niveau que chaque gestionnaire de session (`StreamableHTTPSessionManager.app`) fait
tourner pour chaque session ; et les fermetures de sessions, comptées au même endroit.

**1. La dernière réponse reste vivante tant que la session l'est.** La tâche
`message_router` du transport (`mcp/server/streamable_http.py`, `connect`) boucle sur
`async for session_message in write_stream_reader` : ses variables locales retiennent le
DERNIER message qu'elle a acheminé jusqu'à l'arrivée du suivant. Une session qui s'arrête
après un `tools/list` (~276 Ko) garde ainsi ~1,3 Mo de tas Python et ~3 Mo de RSS, jusqu'à
sa fermeture pour inactivité (`borne_session`, 30 min) — de 2 à 6 Mo par session ouverte.
L'enveloppe du flux d'écriture fait suivre chaque RÉPONSE d'un témoin minuscule : une
réponse JSON-RPC à un identifiant qu'aucune requête ne porte, que le routeur prend à la
place de la vraie (il ne la livre à personne : aucun flux de requête ne l'attend, et une
réponse ne va jamais au flux GET autonome). Garanti tant qu'aucun `event_store` n'est
posé (il STOCKERAIT le témoin pour le rejouer) : un gestionnaire qui en porte un refuse le
démarrage.

**2. Une session fermée par `DELETE`** — en 1.28.x, l'SDK ne la retirait jamais de
`_server_instances` / `_session_owners`, et l'enveloppe le faisait à la fin de `run`.
**Corrigé par l'SDK en 1.30.0** (`_discard_session`, quelle que soit la fin de la session :
`DELETE`, inactivité, panne, requête d'ouverture refusée) : ce retrait a été ôté de
l'enveloppe (25/11/2026), qui ne fait plus que COMPTER les fermetures. Son témoin
(`tests/test_sessions_mcp_liberation.py`) prouve désormais que l'SDK la retire seul.

**La ligne de contrôle** (`veiller`) : toutes les `INTERVALLE_VEILLE_S`, une ligne INFO
par processus, par face — sessions ouvertes, sessions terminées encore retenues (0
attendu), fermées pour inactivité et terminées autrement (`DELETE` du client, ou requête
d'ouverture que l'SDK a refusée : il la termine aussi) depuis la ligne précédente. Le
journal de l'SDK (`Created new transport…`, `Session … idle timeout`) dit la même chose
session par session, mais seulement si les journaux `mcp.*` sont au niveau INFO.

L'accroche est éprouvée contre le VRAI SDK par `tests/test_sessions_mcp_liberation.py`, y
compris par un témoin qui rougit le jour où l'SDK corrige lui-même le défaut (l'enveloppe
devient alors inutile : la retirer).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence
from uuid import uuid4

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCMessage, JSONRPCResponse

logger = logging.getLogger(__name__)

INTERVALLE_VEILLE_S = 300


class AccrocheSdkAbsente(RuntimeError):
    """Le gestionnaire de session de l'SDK n'a plus la forme sur laquelle l'enveloppe
    s'accroche : elle serait inerte (ou nuisible). Levé au démarrage."""


@dataclass
class Equipe:
    """Un gestionnaire de session équipé, et ses compteurs de fermetures."""
    nom: str
    gestionnaire: object
    inactivite: int = 0
    delete: int = 0
    _dits: tuple[int, int] = field(default=(0, 0))

    def releve(self) -> str:
        instances = list(self.gestionnaire._server_instances.values())
        retenues = sum(1 for t in instances if t.is_terminated)
        inactivite = self.inactivite - self._dits[0]
        delete = self.delete - self._dits[1]
        self._dits = (self.inactivite, self.delete)
        return (f"{self.nom} ouvertes={len(instances) - retenues} terminees_retenues={retenues} "
                f"fermees_inactivite={inactivite} fermees_delete={delete}")


class _EcritureLiberatrice:
    """Le flux d'écriture d'une session, dont chaque réponse est suivie d'un témoin qui
    la chasse des variables locales du routeur du transport."""

    def __init__(self, flux):
        self._flux = flux
        self._temoin = SessionMessage(message=JSONRPCMessage(JSONRPCResponse(
            jsonrpc="2.0", id=f"oto-liberation-{uuid4().hex}", result={})))

    def __getattr__(self, nom):
        return getattr(self._flux, nom)

    async def __aenter__(self):
        await self._flux.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._flux.__aexit__(*exc)

    async def send(self, message) -> None:
        await self._flux.send(message)
        if isinstance(message.message.root, JSONRPCResponse | JSONRPCError):
            try:
                await self._flux.send(self._temoin)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                # Session fermée entre la réponse et le témoin : son routeur est parti
                # avec ses locales, il n'y a plus rien à libérer.
                pass


class _ServeurLiberateur:
    """Le serveur bas niveau d'un gestionnaire, dont `run` (une session) écrit par
    `_EcritureLiberatrice` et compte, à sa fin, comment la session s'est fermée (l'SDK
    1.30 la retire lui-même)."""

    def __init__(self, serveur, equipe: Equipe):
        self._serveur = serveur
        self._equipe = equipe

    def __getattr__(self, nom):
        return getattr(self._serveur, nom)

    async def run(self, read_stream, write_stream, *args, **kwargs):
        instances = self._equipe.gestionnaire._server_instances
        # Le transport est inscrit AVANT le lancement de sa tâche ; il se reconnaît au
        # flux qu'il a lui-même cédé. Absent en mode sans état : rien à compter.
        transport = next((t for t in instances.values()
                          if getattr(t, "_write_stream", None) is write_stream), None)
        try:
            return await self._serveur.run(
                read_stream, _EcritureLiberatrice(write_stream), *args, **kwargs)
        finally:
            if transport is not None:
                self._fin_de_session(transport)

    def _fin_de_session(self, transport) -> None:
        # Compter seulement : l'SDK (1.30) retire la session lui-même, dans tous les cas.
        portee = transport.idle_scope
        if portee is not None and portee.cancel_called:
            self._equipe.inactivite += 1
            return
        if not transport.is_terminated:
            return                                 # arrêt du processus, ou panne
        self._equipe.delete += 1                   # DELETE, ou ouverture refusée


def _verifier(gestionnaire) -> None:
    manque = [nom for nom in ("_server_instances",)
              if not isinstance(getattr(gestionnaire, nom, None), dict)]
    if not callable(getattr(getattr(gestionnaire, "app", None), "run", None)):
        manque.append("app.run")
    if manque:
        raise AccrocheSdkAbsente(
            f"{type(gestionnaire).__name__} ne porte plus {', '.join(manque)} : "
            "la libération des sessions MCP ne s'appliquerait pas.")
    if getattr(gestionnaire, "event_store", None) is not None:
        raise AccrocheSdkAbsente(
            "un `event_store` est posé : il stockerait le témoin de libération des "
            "sessions MCP et le rejouerait aux clients.")


def equiper(faces: Iterable, noms: Sequence[str] = ()) -> list[Equipe]:
    """Enveloppe le serveur de chaque gestionnaire de session à état des faces (après
    le démarrage de leur cycle de vie). Idempotent. Lève `AccrocheSdkAbsente`."""
    from .borne_session import gestionnaires
    equipes = []
    for i, face in enumerate(faces):
        nom = noms[i] if i < len(noms) else f"face{i}"
        for g in gestionnaires(face):
            if getattr(g, "stateless", False):
                continue                           # sans session, rien à retenir
            if isinstance(getattr(g, "app", None), _ServeurLiberateur):
                equipes.append(g.app._equipe)
                continue
            _verifier(g)
            equipe = Equipe(nom, g)
            g.app = _ServeurLiberateur(g.app, equipe)
            equipes.append(equipe)
    return equipes


def ligne(equipes: Iterable[Equipe]) -> str:
    return "sessions MCP : " + " ; ".join(e.releve() for e in equipes)


async def veiller(equipes: Sequence[Equipe], intervalle: float = INTERVALLE_VEILLE_S) -> None:
    """Tâche de fond : une ligne INFO toutes les `intervalle` secondes. Aucun I/O."""
    if not equipes:
        return
    while True:
        await asyncio.sleep(intervalle)
        logger.info("%s (sur %d s)", ligne(equipes), int(intervalle))

"""Garde ASGI : l'INACTIVITÉ d'une session MCP (#1232, lot 4).

Une borne GLOBALE du processus (`bornes_processus`), `OTO_BORNE_SESSION_INACTIVE_S`
(30 min) : une session MCP sans requête depuis ce délai est fermée, et sa mémoire rendue.
Aucun état par principal, aucun plafond de nombre : la règle du 13/10/2026 n'en veut
pas.

**En application** (`OTO_DEBIT_MODE=enforce`), c'est la borne de l'SDK `mcp` lui-même,
`StreamableHTTPSessionManager.session_idle_timeout` : chaque requête d'une session
repousse son échéance, l'échéance passée ferme la session, et une requête qui revient
ensuite reçoit le `404 Session not found` du transport — le client se réinitialise.
fastmcp ne transmet pas ce réglage à l'SDK : il est posé sur le gestionnaire de session
de chaque face (`app.session_manager`) quand le cycle de vie annonce le démarrage, c'est-
à-dire après la construction du gestionnaire et avant la première requête. Un SDK qui ne
porterait plus ce réglage refuse le démarrage (jamais une borne annoncée et inerte).

**La mesure** (toujours, sauf `off`) : ce que l'SDK n'expose pas, cette couche le voit —
l'en-tête `Mcp-Session-Id` de chaque requête et de chaque réponse. Elle retient, par
SESSION (pas par principal), l'instant de sa dernière requête ; une requête qui revient
après plus que la borne est une session que l'application aurait fermée : c'est le coût
de la borne, compté au palmarès des refus (limiteur `session`, classe `mcp`, clé =
empreinte du jeton ou de l'adresse) et journalisé. Au plus `_SESSIONS_MAX` sessions
suivies (au-delà, la plus anciennement vue sort) ; un `DELETE` en retire une.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from . import bornes_processus, limite_refus_top

logger = logging.getLogger(__name__)

_ENTETE = b"mcp-session-id"
_SESSIONS_MAX = 50_000


class SdkSansBorneDeSession(RuntimeError):
    """Le gestionnaire de session de l'SDK `mcp` ne porte pas `session_idle_timeout` :
    la borne ne serait pas appliquée. Levé au démarrage, en application seulement."""


def armer(faces: Iterable, secondes: int) -> int:
    """Pose `session_idle_timeout` sur le gestionnaire de session de chaque face qui en
    a un (après le démarrage de son cycle de vie). Rend le nombre de faces armées."""
    armees = 0
    for face in faces:
        gestionnaire = getattr(face, "session_manager", None)
        if gestionnaire is None:
            continue
        if getattr(gestionnaire, "stateless", False):
            continue                    # sans session, rien à borner
        if not hasattr(gestionnaire, "session_idle_timeout"):
            raise SdkSansBorneDeSession(
                f"{type(gestionnaire).__name__} ne porte pas `session_idle_timeout` : "
                "la borne d'inactivité des sessions MCP ne s'appliquerait pas.")
        gestionnaire.session_idle_timeout = float(secondes)
        armees += 1
    return armees


class BorneSession:
    """Middleware ASGI : mesure (et, en application, arme) la borne d'inactivité des
    sessions MCP. Cf. la docstring du module."""

    def __init__(self, app, faces: Iterable = (), reglages=None, horloge=time.monotonic):
        from .subdomain_project import _client_ip
        self.app = app
        self._ip = _client_ip
        self._faces = tuple(faces)
        self._r = reglages or bornes_processus.reglages()
        self._horloge = horloge
        self._vues: dict[bytes, float] = {}

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            return await self._cycle_de_vie(scope, receive, send)
        borne = self._r.session_inactive_s
        if scope.get("type") != "http" or not self._r.actif or borne <= 0:
            return await self.app(scope, receive, send)

        entetes = scope.get("headers") or []
        session = next((v for k, v in entetes if k == _ENTETE and v), None)
        maintenant = self._horloge()
        if session is not None:
            if scope.get("method") == "DELETE":
                self._vues.pop(session, None)
            else:
                derniere = self._vues.get(session)
                if derniere is not None and maintenant - derniere > borne:
                    self._depasser(scope, entetes, maintenant - derniere, borne)
                self._noter(session, maintenant)
            return await self.app(scope, receive, send)

        async def envoyer(message):
            # Une session NAÎT dans la réponse (`initialize`) : son horloge part là.
            if message.get("type") == "http.response.start":
                nee = next((v for k, v in message.get("headers") or []
                            if k.lower() == _ENTETE and v), None)
                if nee is not None:
                    self._noter(nee, self._horloge())
            await send(message)

        await self.app(scope, receive, envoyer)

    def _noter(self, session: bytes, instant: float) -> None:
        self._vues.pop(session, None)          # réinsérée en queue : l'éviction vise l'inactive
        self._vues[session] = instant
        while len(self._vues) > _SESSIONS_MAX:
            del self._vues[next(iter(self._vues))]

    def _depasser(self, scope, entetes, inactive_s: float, borne: int) -> None:
        jeton = next((v for k, v in entetes if k == b"authorization" and v), None)
        cle = limite_refus_top.empreinte(
            jeton.decode("latin-1") if jeton is not None
            else self._ip(scope, dict(entetes)))
        bornes_processus.depasser(
            limite_refus_top.SESSION, "mcp", cle,
            f"session MCP reprise après {int(inactive_s)} s d'inactivité "
            f"(borne {borne} s)", self._r.mode)

    async def _cycle_de_vie(self, scope, receive, send):
        """En application, arme les gestionnaires de session au démarrage annoncé."""
        if not (self._r.applique and self._r.session_inactive_s > 0):
            return await self.app(scope, receive, send)

        async def envoyer(message):
            if message.get("type") == "lifespan.startup.complete":
                try:
                    armees = armer(self._faces, self._r.session_inactive_s)
                except SdkSansBorneDeSession as e:
                    # `startup.failed` et non une exception seule : uvicorn, en
                    # `lifespan=auto`, prendrait celle-ci pour un cycle de vie absent
                    # et servirait quand même.
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    raise
                logger.info("borne de session MCP : %d s d'inactivité, %d face(s)",
                            self._r.session_inactive_s, armees)
            await send(message)

        await self.app(scope, receive, envoyer)

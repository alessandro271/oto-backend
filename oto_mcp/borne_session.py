"""Garde ASGI : l'INACTIVITÉ d'une session MCP (#1232, lot 4).

Une borne GLOBALE du processus (`bornes_processus`), `OTO_BORNE_SESSION_INACTIVE_S`
(30 min) : une session MCP sans requête depuis ce délai est fermée, et sa mémoire rendue.
Aucun état par principal, aucun plafond de nombre : la règle du 13/10/2026 n'en veut
pas.

**Armée hors `off`** (`report` comme `enforce`) : c'est la borne de l'SDK `mcp` lui-même,
`StreamableHTTPSessionManager.session_idle_timeout` : chaque requête d'une session
repousse son échéance, l'échéance passée ferme la session, et une requête qui revient
ensuite reçoit le `404 Session not found` du transport — le client se réinitialise.
Ce n'est pas un refus mais l'hygiène mémoire du processus : une session jamais fermée
garde sa mémoire (de l'ordre du Mo, `tools/list` compris) jusqu'au redémarrage, et la
plupart des clients n'envoient jamais de `DELETE`.
fastmcp ne transmet pas ce réglage à l'SDK, et ne pose pas le gestionnaire sur
l'application Starlette mais sur l'ASGI de la route `/mcp` (`StreamableHTTPASGIApp`) :
`armer` le cherche là, quand le cycle de vie annonce le démarrage (après la construction
du gestionnaire, avant la première requête). Une borne positive qui ne trouve pas de
gestionnaire sur UNE des faces, ou un SDK qui ne porterait plus ce réglage, refuse le démarrage (jamais
une borne annoncée et inerte : avant ce correctif, le journal disait « 0 face(s) »).

**Équipée dans tous les modes** (`sessions_mcp`, au même instant) : chaque gestionnaire à
état libère la dernière réponse de ses sessions ouvertes et retire celles que `DELETE` a
terminées (l'SDK les garde sinon pour toujours) ; une ligne INFO de contrôle les compte
toutes les 5 min. Une accroche absente refuse le démarrage, comme la borne.

**La mesure** (toujours, sauf `off`) : ce que l'SDK n'expose pas, cette couche le voit —
l'en-tête `Mcp-Session-Id` de chaque requête et de chaque réponse. Elle retient, par
SESSION (pas par principal), l'instant de sa dernière requête ; une requête qui revient
après plus que la borne est une session que l'application aurait fermée : c'est le coût
de la borne, compté au palmarès des refus (limiteur `session`, classe `mcp`, clé =
empreinte du jeton ou de l'adresse) et journalisé. Au plus `_SESSIONS_MAX` sessions
suivies (au-delà, la plus anciennement vue sort) ; un `DELETE` en retire une.
"""
from __future__ import annotations

import asyncio
import contextlib
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


def gestionnaires(face) -> list:
    """Les gestionnaires de session d'une face : posé sur la face elle-même (banc), ou
    sur l'ASGI d'une de ses routes — là où fastmcp le pose (`StreamableHTTPASGIApp`)."""
    direct = getattr(face, "session_manager", None)
    if direct is not None:
        return [direct]
    trouves = []
    routes = getattr(getattr(face, "router", None), "routes", None) or ()
    for route in routes:
        for cible in (getattr(route, "app", None), getattr(route, "endpoint", None)):
            # La face authentifiée enveloppe l'ASGI de `/mcp` (`RequireAuthMiddleware`,
            # qui le garde en `.app`) : on descend la chaîne des `.app`, bornée.
            for _ in range(8):
                if cible is None:
                    break
                gestionnaire = getattr(cible, "session_manager", None)
                if gestionnaire is not None:
                    if all(g is not gestionnaire for g in trouves):
                        trouves.append(gestionnaire)
                    break
                cible = getattr(cible, "app", None)
    return trouves


def armer(faces: Iterable, secondes: int) -> int:
    """Pose `session_idle_timeout` sur chaque gestionnaire de session des faces (après
    le démarrage de leur cycle de vie). Rend le nombre de gestionnaires armés. Lève
    `SdkSansBorneDeSession` si UNE face n'a pas de gestionnaire : elle serait inerte."""
    armees = 0
    for face in faces:
        trouves = gestionnaires(face)
        if not trouves:
            raise SdkSansBorneDeSession(
                f"aucun gestionnaire de session MCP trouvé sur la face {type(face).__name__} : "
                "la borne d'inactivité des sessions ne s'y appliquerait pas (fastmcp "
                "l'a-t-il déplacé ?).")
        for gestionnaire in trouves:
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

    def __init__(self, app, faces: Iterable = (), reglages=None, horloge=time.monotonic,
                 noms_faces: Iterable[str] = ()):
        from .subdomain_project import _client_ip
        self.app = app
        self._ip = _client_ip
        self._faces = tuple(faces)
        self._noms_faces = tuple(noms_faces)
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
            f"(borne {borne} s)",
            # Armée hors `off` : la session a bien été fermée, quel que soit le mode.
            bornes_processus.ENFORCE)

    async def _cycle_de_vie(self, scope, receive, send):
        """Au démarrage annoncé : hors `off`, arme les gestionnaires de session ; dans
        tous les modes, les équipe (`sessions_mcp` : libération de la dernière réponse,
        retrait des sessions terminées) et lance la ligne de contrôle, arrêtée avec le
        cycle de vie."""
        from . import sessions_mcp
        veille = None

        async def envoyer(message):
            nonlocal veille
            if message.get("type") == "lifespan.startup.complete":
                try:
                    if self._r.actif and self._r.session_inactive_s > 0:
                        armees = armer(self._faces, self._r.session_inactive_s)
                        logger.info("borne de session MCP : %d s d'inactivité, "
                                    "%d gestionnaire(s) armé(s)",
                                    self._r.session_inactive_s, armees)
                    equipes = sessions_mcp.equiper(self._faces, self._noms_faces)
                except (SdkSansBorneDeSession, sessions_mcp.AccrocheSdkAbsente) as e:
                    # `startup.failed` et non une exception seule : uvicorn, en
                    # `lifespan=auto`, prendrait celle-ci pour un cycle de vie absent
                    # et servirait quand même.
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    raise
                logger.info("sessions MCP : %d gestionnaire(s) équipé(s), ligne de "
                            "contrôle toutes les %d s", len(equipes),
                            sessions_mcp.INTERVALLE_VEILLE_S)
                veille = asyncio.create_task(sessions_mcp.veiller(equipes))
            await send(message)

        try:
            await self.app(scope, receive, envoyer)
        finally:
            if veille is not None:
                veille.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await veille

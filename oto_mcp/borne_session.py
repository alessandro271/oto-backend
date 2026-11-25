"""Garde ASGI : l'INACTIVITÉ d'une session MCP (#1232, lot 4).

Une borne GLOBALE du processus (`bornes_processus`), `OTO_BORNE_SESSION_INACTIVE_S`
(30 min) : une session MCP sans requête depuis ce délai est fermée, et sa mémoire rendue.
Aucun état par principal. La règle du 13/10/2026 ne veut pas de plafond de nombre ; UNE
exception depuis le 25/11/2026, le plafond de sessions ouvertes de l'SDK `mcp` 1.30
(`plafonner`, ci-dessous) : global, sans état par principal, gardé parce que c'est lui
qui borne la rétention des sessions dans l'SDK (CVE-2026-59951).

**Armée hors `off`** (`report` comme `enforce`) : c'est la borne de l'SDK `mcp` lui-même,
`StreamableHTTPSessionManager.session_idle_timeout`. Depuis l'SDK 1.30, l'inactivité se
compte à partir de la FIN de la dernière requête en cours : tant qu'une requête est
servie, ou qu'un flux GET reste ouvert, la session ne vieillit pas (en 1.28, chaque
requête repoussait l'échéance, flux ouvert ou non). L'échéance passée ferme la session,
et une requête qui revient ensuite reçoit le `404 Session not found` du transport — le
client se réinitialise.
Ce n'est pas un refus mais l'hygiène mémoire du processus : une session jamais fermée
garde sa mémoire (de l'ordre du Mo, `tools/list` compris) jusqu'au redémarrage, et la
plupart des clients n'envoient jamais de `DELETE`.
fastmcp ne transmet pas ce réglage à l'SDK, et ne pose pas le gestionnaire sur
l'application Starlette mais sur l'ASGI de la route `/mcp` (`StreamableHTTPASGIApp`) :
`armer` le cherche là, quand le cycle de vie annonce le démarrage (après la construction
du gestionnaire, avant la première requête). Une borne positive qui ne trouve pas de
gestionnaire sur UNE des faces, ou un SDK qui ne porterait plus ce réglage, refuse le démarrage (jamais
une borne annoncée et inerte : avant ce correctif, le journal disait « 0 face(s) »).

**Plafonnée dans tous les modes** (`plafonner`, au même instant, AVANT `armer`) : l'SDK
1.30 applique de lui-même trois défauts de transport, quel que soit notre interrupteur ;
on les pose explicitement depuis `bornes_processus` :

- le corps d'une requête `/mcp` : `max_request_body_size` = `OTO_BORNE_CORPS_MCP_OCTETS`
  (32 MiB, au-dessus du proxy : l'SDK ne refuse rien de ce que le proxy laisse passer).
  L'SDK fige ce réglage à la construction, dans `asgi_app`
  (`RequestBodyLimitMiddleware(_handle_request, n)`), et c'est `asgi_app` que sert
  `handle_request`, ce qu'appelle l'ASGI de fastmcp : on remplace le réglage ET on
  reconstruit `asgi_app` autour du même `_handle_request` ;
- le nombre de sessions ouvertes à la fois : `max_sessions` = `OTO_BORNE_SESSIONS_MCP_MAX`
  (10 000, le défaut de l'SDK ; lu à chaque ouverture → `503` au-delà) ;
- l'inactivité : en `off`, ou avec `OTO_BORNE_SESSION_INACTIVE_S=0`, `session_idle_timeout`
  est remis à `None` (1.30 l'arme à 30 min par défaut ; 1.28 ne l'armait pas) — « seul
  `off` la coupe » et « `0` désarme » restent vrais. Les sessions ne sortent alors que par
  `DELETE` : le plafond finit par se remplir.

Un gestionnaire qui n'a pas la forme attendue refuse le démarrage (`SdkSansBorneDeSession`).

**Équipée dans tous les modes** (`sessions_mcp`, au même instant) : chaque gestionnaire à
état libère la dernière réponse de ses sessions ouvertes ; une ligne INFO de contrôle les
compte toutes les 5 min. Une accroche absente refuse le démarrage, comme la borne.

**La mesure** (toujours, sauf `off`) : ce que l'SDK n'expose pas, cette couche le voit
(une APPROXIMATION depuis 1.30 : elle date la dernière requête, pas la fin du dernier
flux ; une session tenue par un flux GET ouvert peut être comptée à tort) —
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


def _faces_et_gestionnaires(faces: Iterable):
    for face in faces:
        trouves = gestionnaires(face)
        if not trouves:
            raise SdkSansBorneDeSession(
                f"aucun gestionnaire de session MCP trouvé sur la face {type(face).__name__} : "
                "les bornes des sessions ne s'y appliqueraient pas (fastmcp "
                "l'a-t-il déplacé ?).")
        yield from trouves


def plafonner(faces: Iterable, reglages) -> int:
    """Pose sur chaque gestionnaire de session des faces (après le démarrage de leur
    cycle de vie) la taille max du corps, le plafond de sessions ouvertes et, hors
    borne d'inactivité, son désarmement. DANS TOUS LES MODES. Rend le nombre de
    gestionnaires plafonnés. Lève `SdkSansBorneDeSession` si un réglage ne s'y
    appliquerait pas. Idempotent."""
    try:
        from mcp.server.transport_security import RequestBodyLimitMiddleware
    except ImportError as e:            # pragma: no cover — SDK < 1.30, refusé par le manifeste
        raise SdkSansBorneDeSession(
            "l'SDK `mcp` servi n'a pas `RequestBodyLimitMiddleware` (1.30 attendu) : la "
            "taille max du corps des requêtes MCP ne serait pas posée.") from e
    desarmer = not (reglages.actif and reglages.session_inactive_s > 0)
    plafonnes = 0
    for g in _faces_et_gestionnaires(faces):
        nom = type(g).__name__
        enveloppe = getattr(g, "asgi_app", None)
        interne = getattr(g, "_handle_request", None)
        if (not hasattr(g, "max_request_body_size")
                or not isinstance(enveloppe, RequestBodyLimitMiddleware)
                or interne is None or enveloppe.app != interne):
            raise SdkSansBorneDeSession(
                f"{nom} ne sert plus ses requêtes par `asgi_app = "
                "RequestBodyLimitMiddleware(_handle_request, …)` : la taille max du corps "
                "des requêtes MCP ne s'appliquerait pas.")
        g.max_request_body_size = reglages.corps_mcp_octets
        g.asgi_app = RequestBodyLimitMiddleware(enveloppe.app, reglages.corps_mcp_octets)
        if not getattr(g, "stateless", False):
            for attendu in ("max_sessions", "session_idle_timeout"):
                if not hasattr(g, attendu):
                    raise SdkSansBorneDeSession(
                        f"{nom} ne porte pas `{attendu}` : les bornes des sessions MCP "
                        "ne s'appliqueraient pas.")
            g.max_sessions = reglages.sessions_max
            if desarmer:
                g.session_idle_timeout = None
        plafonnes += 1
    return plafonnes


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
            # Armée hors `off` : la session a été fermée par l'SDK, quel que soit le mode
            # (sauf si un flux GET ouvert l'a tenue : cf. la docstring du module).
            bornes_processus.ENFORCE)

    async def _cycle_de_vie(self, scope, receive, send):
        """Au démarrage annoncé : dans tous les modes, plafonne les gestionnaires de
        session (`plafonner`) ; hors `off`, les arme ; dans tous les modes, les équipe (`sessions_mcp` : libération de la dernière réponse,
        retrait des sessions terminées) et lance la ligne de contrôle, arrêtée avec le
        cycle de vie."""
        from . import sessions_mcp
        veille = None

        async def envoyer(message):
            nonlocal veille
            if message.get("type") == "lifespan.startup.complete":
                try:
                    plafonnes = plafonner(self._faces, self._r)
                    logger.info("transport MCP : corps max %d octets, %d sessions ouvertes "
                                "au plus, %d gestionnaire(s) plafonné(s)",
                                self._r.corps_mcp_octets, self._r.sessions_max, plafonnes)
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

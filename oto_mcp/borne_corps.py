"""Garde ASGI : la TAILLE DU CORPS de toute requête HTTP (#1232, lot 4).

Une borne GLOBALE du processus (`bornes_processus`) : `OTO_BORNE_CORPS_OCTETS` (4 Mo) pour
tout ce que sert le processus, `OTO_BORNE_CORPS_UPLOAD_OCTETS` (110 Mo) sur les routes
d'upload. Une route d'upload se reconnaît à sa DÉCLARATION, jamais à une liste de
chemins : son handler porte un `ContratDeRoute` dont le corps, pour ce verbe, déclare un
type de média autre que JSON (multipart, octets bruts). La table de routes vivante est
lue une fois, à la construction.

**Ce qu'elle mesure.** Le `Content-Length` annoncé, s'il y en a un ; sinon (envoi
`chunked`) les octets reçus, compteur à la main, sans rien mettre en tampon : un corps
qui passe ne coûte qu'une addition par morceau. Un dépassement va au palmarès des refus
(limiteur `corps`, classe `defaut` | `upload`, clé = empreinte du jeton porté ou de
l'adresse, comme la limite de débit) et au journal — jamais le chemin, qui peut porter un
secret.

**En application** (`OTO_DEBIT_MODE=enforce`) : `413 payload_too_large`, enveloppe
d'erreur ordinaire, CORS et `X-Oto-Version`. Annoncé trop gros : refusé avant d'appeler
la suite. En flux : la lecture s'arrête au premier morceau de trop (l'application voit
le client partir), ce qu'elle aurait répondu est écarté, et le 413 part à sa place.
En mesure (`report`, défaut) : rien ne change pour la requête.

**Où elle est posée** (`server.build_root_app`) : sous la limite de débit (un débit
refusé ne lit pas son corps), au-dessus du compteur des refus du transport et du
dispatch par Host — les deux faces MCP et toute la face REST d'un seul geste.

Couche applicative d'un dispositif à deux couches : le proxy devant l'instance peut
aussi borner les corps ; celle-ci tient la borne là où le proxy n'en pose pas, et la
mesure.
"""
from __future__ import annotations

import logging
from typing import Iterable

from . import bornes_processus, limite_debit, limite_refus_top

logger = logging.getLogger(__name__)

DEFAUT, UPLOAD = "defaut", "upload"


def routes_upload(routes: Iterable) -> tuple:
    """(motif du chemin, verbes) des routes dont le CONTRAT déclare un corps non JSON —
    lu sur la table vivante (`route.endpoint.contrat`, posé par le handler)."""
    from .capabilities._types import ContratDeRoute
    trouvees = []
    for route in routes or ():
        contrat = getattr(getattr(route, "endpoint", None), "contrat", None)
        motif = getattr(route, "path_regex", None)
        if not isinstance(contrat, ContratDeRoute) or motif is None:
            continue
        verbes = frozenset(verbe.upper() for verbe, medias in (contrat.corps or {}).items()
                           if any(m != "application/json" for m in medias))
        if verbes:
            trouvees.append((motif, verbes))
    return tuple(trouvees)


class _Coupe(Exception):
    """Le corps en flux a dépassé la borne, en application : la lecture s'arrête."""


class BorneCorps:
    """Middleware ASGI : borne la taille du corps de chaque requête HTTP. Cf. la
    docstring du module."""

    def __init__(self, app, routes: Iterable = (), reglages=None):
        from .subdomain_project import _client_ip
        self.app = app
        self._ip = _client_ip
        self._r = reglages or bornes_processus.reglages()
        self._uploads = routes_upload(routes)

    def _classe(self, methode: str, chemin: str) -> str:
        for motif, verbes in self._uploads:
            if methode in verbes and motif.match(chemin):
                return UPLOAD
        return DEFAUT

    def _plafond(self, classe: str) -> int:
        return self._r.corps_upload_octets if classe == UPLOAD else self._r.corps_octets

    def _depasser(self, scope, classe: str, taille: str) -> None:
        entetes = scope.get("headers") or []
        jeton = next((v for k, v in entetes if k == b"authorization" and v), None)
        cle = limite_refus_top.empreinte(
            jeton.decode("latin-1") if jeton is not None
            else self._ip(scope, dict(entetes)))
        bornes_processus.depasser(
            limite_refus_top.CORPS, classe, cle,
            f"corps {taille} au-delà de {self._plafond(classe)} octets "
            f"({scope.get('method', '')})", self._r.mode)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self._r.actif:
            return await self.app(scope, receive, send)
        classe = self._classe(scope.get("method", ""), scope.get("path") or "")
        plafond = self._plafond(classe)
        if plafond <= 0:
            return await self.app(scope, receive, send)

        annonce = next((v for k, v in scope.get("headers") or []
                        if k == b"content-length"), b"")
        if annonce.isdigit() and int(annonce) > plafond:
            self._depasser(scope, classe, f"annoncé de {int(annonce)} octets")
            if self._r.applique:
                return await self._refuser(scope, send, plafond)
            return await self.app(scope, receive, send)

        etat = {"recu": 0, "signale": False, "coupe": False, "demarre": False}

        async def recevoir():
            if etat["coupe"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                etat["recu"] += len(message.get("body") or b"")
                if etat["recu"] > plafond and not etat["signale"]:
                    etat["signale"] = True
                    self._depasser(scope, classe, "reçu en flux")
                    if self._r.applique and not etat["demarre"]:
                        etat["coupe"] = True
                        return {"type": "http.disconnect"}
            return message

        async def envoyer(message):
            if etat["coupe"]:
                return                     # la réponse de l'application est écartée
            if message.get("type") == "http.response.start":
                etat["demarre"] = True
            await send(message)

        try:
            await self.app(scope, recevoir, envoyer)
        except Exception:
            if not etat["coupe"]:
                raise
            # L'application a vu le client partir (c'est nous qui avons coupé) : son
            # exception est la suite de la coupe, pas une panne. Le 413 part ci-dessous.
            logger.debug("corps coupé : l'application a levé après la coupe",
                         exc_info=True)
        if etat["coupe"]:
            await self._refuser(scope, send, plafond)

    async def _refuser(self, scope, send, plafond: int) -> None:
        await limite_debit.envoyer_refus(scope, send, 413, {
            "error": "payload_too_large",
            "detail": f"corps de requête trop gros : au plus {plafond} octets.",
            "details": {"retryable": False, "limit_bytes": plafond},
        })

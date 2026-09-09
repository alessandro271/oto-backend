"""Route de retour du consentement Instagram — la seule pièce écrite à la main.

Le `/start` n'est PAS ici : c'est le seam commun (`connectors/flow`), dont
`auth/instagram_meta._start_flow` est la déclaration. Seul le callback reste une
route : Meta y redirige un NAVIGATEUR, sans en-tête d'auth et avec un 302, ce
qu'un contrat de capacité ne peut pas exprimer. Même forme que `api/salesforce.py`.

L'identité de la personne vient du `state` signé, pas d'une session : c'est tout
ce dont ce handler dispose, et c'est pourquoi le state porte `sub` et `org`.

⚠️ **Le refus le plus fréquent de ce flux n'est pas un bug, c'est le régime des
applications Meta non publiées** : tant qu'elle n'a pas passé l'App Review, seuls
les comptes INVITÉS comme testeurs peuvent consentir. Meta renvoie alors un
`error=access_denied`, indistinguable d'un vrai refus de la personne. Le message
nomme donc les deux causes, dans cet ordre — parce que la première est la nôtre à
régler, et que présenter « tu as refusé » à quelqu'un qui a cliqué « Autoriser »
le laisse sans recours.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import flow as oauth_flow
from ..auth import instagram_meta as ig_auth

logger = logging.getLogger(__name__)

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]

#: Le motif que Meta rend quand la personne n'est pas testeuse de l'application.
#: Il est le MÊME que celui d'un vrai refus : `access_denied`. On ne peut donc pas
#: les distinguer — d'où un message qui nomme les deux, sans en affirmer une.
_REFUS = "access_denied"


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, return_app: str = "", org_id: int | None = None) -> str:
        """Où renvoyer le navigateur : sur la fiche du connecteur, dépliée.

        `connector=` est le deep-link lu par le dashboard, `connect=` dit CE QUI
        S'EST PASSÉ — sans lui, une personne revient devant un écran muet et ne
        peut pas savoir si son geste a abouti."""
        return oauth_flow.connector_return_url(
            return_app, ig_auth.CONNECTOR, etat, org=org_id)

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        erreur = request.query_params.get("error")
        parsed = ig_auth.verify_state(state) if state else None
        if not parsed:
            # State absent, expiré ou altéré : on n'a NI org NI front de retour
            # (ils vivent DANS ce state qu'on vient d'échouer à lire). Dégradation
            # acceptée vers le défaut, seul cas où ce module ne peut pas faire mieux.
            logger.info("instagram_meta : retour de consentement sans state lisible")
            return RedirectResponse(_retour("error"), status_code=302)
        sub, org_id, return_app = parsed
        if erreur or not code:
            # On JOURNALISE le motif rendu par Meta (il n'est pas secret) et on
            # renvoie l'utilisatrice sur sa fiche, où le message ci-dessous
            # l'attend. Voir `oto_mcp/connectors/docs/instagram_meta.md`.
            logger.info("instagram_meta : consentement non abouti (sub=%s, motif=%s)",
                        sub, erreur or "code absent")
            return RedirectResponse(
                _retour("forbidden" if erreur == _REFUS else "error",
                        return_app, org_id), status_code=302)

        def _echanger_et_ranger() -> dict:
            grant = ig_auth._coeur().connect(
                ig_auth.app(), code,
                oauth_flow.redirect_uri(ig_auth._CALLBACK_PATH))
            return ig_auth.persist_grant(sub, org_id, grant)

        try:
            # DB + HTTP synchrones hors de la boucle : ce handler est `async def`,
            # et l'échange parle trois fois à Meta. Appelé nûment il fige tout le
            # processus le temps que l'amont réponde (oto-backend#867).
            await run_in_threadpool(_echanger_et_ranger)
        except Exception:
            # Sans trace ici, un échec de connexion est INDIAGNOSTICABLE : le
            # client ne voit qu'un `connect=error`. On journalise le traceback,
            # jamais le `code` ni le jeton.
            logger.exception("instagram_meta : retour de consentement en échec "
                             "(sub=%s org=%s)", sub, org_id)
            return RedirectResponse(_retour("error", return_app, org_id),
                                    status_code=302)
        return RedirectResponse(_retour("connected", return_app, org_id),
                                status_code=302)

    return [Route(ig_auth._CALLBACK_PATH, callback, methods=["GET"])]

"""Return route of the LinkedIn Ads connection — the only hand-written piece.

Same shape as `api/jefacture.py`: `/start` goes through the common seam
(`connectors/flow`), only the callback is a route (LinkedIn redirects a
BROWSER, with no auth header). The identity comes from the signed `state`.

In order: state read, state CONSUMED (single use), the person's refusal, membership
and connector RE-CHECKED (`forbidden`), then exchange and storage. The code alone is
worth nothing without the application's secret (confidential client): its query
keys `code` and `state` are masked by NAME in the access log
(`journal_secrets.CLES_DE_REQUETE_SECRETES`), like every OAuth return.
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
from ..auth import linkedin_ads as li_auth

logger = logging.getLogger(__name__)

#: LinkedIn says a refusal of the person with its own codes (3-legged OAuth doc), not
#: `access_denied`: both read as the provider's refusal (`connect=denied`).
_REFUS_LINKEDIN = {"user_cancelled_login": oauth_flow.REFUS_FOURNISSEUR,
                   "user_cancelled_authorize": oauth_flow.REFUS_FOURNISSEUR}

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, return_app: str = "", org_id: int | None = None) -> str:
        return oauth_flow.connector_return_url(
            return_app, li_auth.CONNECTOR, etat, org=org_id)

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        erreur = request.query_params.get("error")
        parsed = li_auth.verify_state(state) if state else None
        if not parsed:
            logger.info("linkedin_ads: connection return without a readable state")
            return RedirectResponse(_retour("error"), status_code=302)
        sub, org_id, return_app = parsed["sub"], parsed["org"], parsed["app"]
        if not await run_in_threadpool(oauth_flow.consume_state, li_auth._AUD, parsed):
            logger.warning("linkedin_ads: connection return with a replayed state (sub=%s)",
                           sub)
            return RedirectResponse(_retour("error", return_app, org_id), status_code=302)
        if erreur or not code:
            logger.info("linkedin_ads: connection not completed (sub=%s, reason=%s)",
                        sub, erreur or "missing code")
            return RedirectResponse(
                _retour(oauth_flow.etat_sans_code(_REFUS_LINKEDIN.get(erreur, erreur)),
                        return_app, org_id),
                status_code=302)
        if not await run_in_threadpool(li_auth.still_allowed, parsed):
            logger.warning("linkedin_ads: connection return refused: %s is no longer a "
                           "member of org %s, or the connector is now cut there",
                           sub, org_id)
            return RedirectResponse(_retour("forbidden", return_app, org_id),
                                    status_code=302)

        try:
            # Synchronous DB + HTTP off the event loop (oto-backend#867).
            await run_in_threadpool(li_auth.exchange_and_persist, parsed, code)
        except Exception:
            # We log the traceback, never the `code` or the token.
            logger.exception("linkedin_ads: connection return failed (sub=%s org=%s)",
                             sub, org_id)
            return RedirectResponse(_retour("error", return_app, org_id),
                                    status_code=302)
        return RedirectResponse(_retour("connected", return_app, org_id),
                                status_code=302)

    return [Route(li_auth.CALLBACK_PATH, callback, methods=["GET"])]

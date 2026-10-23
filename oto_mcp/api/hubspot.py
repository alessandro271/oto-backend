"""Return route of the HubSpot consent — the only hand-written piece.

Same shape as `api/meta_ads.py`: `/start` goes through the common seam
(`connectors/flow`, declared in `auth/hubspot.py`), only the callback is a route
(HubSpot redirects a BROWSER, with no auth header). The identity, the level
(member | org | group) and the front to send back to come from the signed `state`,
which is single-use (`jti`): this callback writes a credential.

In order, like the Slack return: state read, state CONSUMED, provider refusal, right to
write at the level RE-CHECKED (`forbidden`), then exchange and storage. The `code` and
the tokens never enter a log line nor the return URL.
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
from ..auth import hubspot as hubspot_auth

logger = logging.getLogger(__name__)

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
            return_app, hubspot_auth.CONNECTOR, etat, org=org_id)

    async def callback(request: Request) -> Response:
        q = request.query_params
        data = hubspot_auth.read_state(q.get("state"))
        if not data:
            logger.info("hubspot: consent return without a readable state")
            return RedirectResponse(_retour("error"), status_code=302)
        sub, org_id, scope = data["sub"], data["org"], data["scope"]
        group_id, return_app = data.get("group"), data["app"]
        # Single use FIRST: a replayed state writes nothing, whatever follows — a
        # refused consent included.
        if not await run_in_threadpool(oauth_flow.consume_state, hubspot_auth.AUD, data):
            logger.warning("hubspot: consent return with a replayed state (sub=%s)", sub)
            return RedirectResponse(_retour("error", return_app, org_id), status_code=302)
        erreur, code = q.get("error"), q.get("code")
        if erreur or not code:
            logger.info("hubspot: consent not completed (sub=%s, reason=%s)",
                        sub, erreur or "code missing")
            return RedirectResponse(
                _retour(oauth_flow.etat_sans_code(erreur), return_app, org_id),
                status_code=302)
        # RE-GUARD of the right to write at this level (ADR 0038): the state lives
        # 10 min, a role can be lost between the click and the return.
        if not await run_in_threadpool(hubspot_auth.can_write, sub, org_id, scope,
                                       group_id):
            logger.warning("hubspot: consent return refused: %s no longer has the right "
                           "to write at level %s (org=%s group=%s)", sub, scope, org_id,
                           group_id)
            return RedirectResponse(_retour("forbidden", return_app, org_id),
                                    status_code=302)

        def _echanger_et_ranger() -> dict:
            # The level's app — the state was refused if its app did not match.
            tokens = hubspot_auth.exchange_code(code, data["kind"])
            return hubspot_auth.persist(sub, org_id, scope, group_id, tokens)

        try:
            # Synchronous DB + HTTP off the loop (oto-backend#867).
            result = await run_in_threadpool(_echanger_et_ranger)
        except oauth_flow.OAuthExchangeRefused as e:
            logger.info("hubspot: exchange refused (sub=%s org=%s): %s", sub, org_id, e)
            return RedirectResponse(_retour("error", return_app, org_id), status_code=302)
        except Exception:
            # The traceback, never the `code` nor a token.
            logger.exception("hubspot: consent return failed (sub=%s scope=%s org=%s)",
                             sub, scope, org_id)
            return RedirectResponse(_retour("error", return_app, org_id), status_code=302)
        logger.info("hubspot: portal connected (hub_id=%s, scope=%s, app=%s, org=%s)",
                    result.get("hub_id"), scope, result.get("app"), org_id)
        return RedirectResponse(_retour("connected", return_app, org_id), status_code=302)

    return [Route(hubspot_auth.CALLBACK_PATH, callback, methods=["GET"])]

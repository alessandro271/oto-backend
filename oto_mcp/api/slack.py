"""The Slack app's hand-written routes. (Its install consent returns through the common
connector return, `api/oauth_returns.py`.)

`POST /api/slack/events` — the app's Events API endpoint (DMs to the bot, uninstall),
hand-written for the same reason as `api/hooks.py`: a free JSON body, signed by Slack,
no JWT. It only adapts; `slack_events.recevoir` decides, and what talks to Slack
runs AFTER the 200 (`slack_events.apres_l_accuse`, background task): Slack waits 3 s.

`POST /api/slack/interactions` — the app's Interactivity endpoint: a press of an Approve /
Cancel / Undo button of the in-app agent's messages (`slack_copilot.py`). A FORM body
(`payload=<json>`), signed by Slack like the events, no JWT; 200 at once, the press is
handled after the ACK.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.routing import Route

from .. import slack_copilot, slack_events
from .hooks import _lire_borne

logger = logging.getLogger(__name__)

EVENTS_PATH = "/api/slack/events"
INTERACTIONS_PATH = "/api/slack/interactions"

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def events(request: Request) -> Response:
        """Slack's deliveries. 200 for everything Slack sent properly, refusals
        included (the person is told in Slack): a non-2xx makes Slack retry, and an
        endpoint that keeps failing gets the app's events switched off.
        `X-Slack-No-Retry` on our own refusals — a retry would not change them."""
        brut = await _lire_borne(request)
        if brut is None:
            return JSONResponse({"error": "payload_too_large"}, status_code=413,
                                headers={"X-Slack-No-Retry": "1"})
        try:
            corps = json.loads(brut) if brut.strip() else None
        except ValueError:
            corps = None
        try:
            rendu, demande = await run_in_threadpool(slack_events.recevoir, brut,
                                                     request.headers, corps)
        except slack_events.Refus as e:
            entetes = {} if e.statut == 503 else {"X-Slack-No-Retry": "1"}
            return JSONResponse({"error": e.code}, status_code=e.statut,
                                headers=entetes)
        except Exception:  # noqa: BLE001 — logged whole; Slack retries a 500
            logger.exception("slack events: delivery failed on our side")
            return JSONResponse({"error": "slack_event_failed"}, status_code=500)
        # The member lookup, the run and the replies wait for the ACK: a sync task,
        # so Starlette runs it in the threadpool, off the loop.
        tache = (BackgroundTask(slack_events.apres_l_accuse, demande)
                 if demande is not None else None)
        return JSONResponse(rendu, status_code=200, background=tache)

    async def interactions(request: Request) -> Response:
        """A button press. Same rule as the events: 200 for everything Slack sent
        properly — a press we will not honour is told to the person in Slack, after the
        ACK — and `X-Slack-No-Retry` on our own refusals."""
        brut = await _lire_borne(request)
        if brut is None:
            return JSONResponse({"error": "payload_too_large"}, status_code=413,
                                headers={"X-Slack-No-Retry": "1"})
        try:
            action = await run_in_threadpool(slack_copilot.recevoir_interaction, brut,
                                             request.headers)
        except slack_events.Refus as e:
            entetes = {} if e.statut == 503 else {"X-Slack-No-Retry": "1"}
            return JSONResponse({"error": e.code}, status_code=e.statut, headers=entetes)
        except Exception:  # noqa: BLE001 — logged whole; Slack shows the press as failed
            logger.exception("slack interactions: delivery failed on our side")
            return JSONResponse({"error": "slack_interaction_failed"}, status_code=500)
        tache = (BackgroundTask(slack_copilot.apres_l_accuse_action, action)
                 if action is not None else None)
        return Response(status_code=200, background=tache)

    return [Route(EVENTS_PATH, events, methods=["POST"]),
            Route(INTERACTIONS_PATH, interactions, methods=["POST"])]

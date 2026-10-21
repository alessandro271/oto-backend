"""Zoho OAuth consent return — the ONLY route still hand-written.

Zoho redirects the user's **browser** here: no auth header (the identity comes from
the signed `state`) and the response is a **302** to the dashboard. A capability
contract (JSON + authz) cannot express that — hence the exception, declared as such
in `tests/test_rest_modules_are_capabilities.py`.

The verbs that go with it (`start`, `modes`) are **capabilities**
(`capabilities/zoho_connect.py`, ADR 0042 §Surface convergence): one declaration,
two derived faces (REST for the dashboard, MCP for the agent), one authz.

A single redirect URI serves ALL THREE Zoho connectors — the connector travels
in the `state` — because a URI is registered byte for byte on the Zoho side: only one
to declare per app instead of three.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import flow as oauth_flow
from ..auth import zoho as zoho_oauth
from .. import roles

logger = logging.getLogger(__name__)

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, return_app: str = "", org_id: "int | None" = None,
               connector: str = "zoho", extra: "dict | None" = None) -> str:
        """Where to send the browser back after the Zoho consent.

        `return_app` = the front that REQUESTED the connection, read back from the signed
        state (`""` when the state could not be read: we then have no way of knowing who
        to call back, an accepted degraded case). While this URL was hard-coded to
        oto-dashboard, a user of a partner front ended their consent
        on another product.

        The default goes through the same maker as every other connector
        (`oauth_flow.return_url`, path `/connectors`). It used to compose
        `/console/connectors` by hand — a path no dashboard route serves: a consent
        that came back there landed on a catch-all page that read nothing of the
        return (`connect=`, `warning=`, `consented_org*`).

        Single OAuth return convention (oto-backend#670): the suffix comes
        from the shared maker `oauth_flow.connector_return_suffix`. The old
        form `?<connector>=connected` / `?zoho=error`, duplicated for the length of a
        notice period, was removed on 17/09/2026 — no reader measured any more (the
        dashboard has read `connect=` since 04/09, and no Zoho connection existed
        at the only partner concerned)."""
        suffix = oauth_flow.connector_return_suffix(connector, etat)
        if extra:
            # What the dashboard SAYS on return (`warning=`, `consented_org=`,
            # `consented_org_id=`): named codes and the member's own Zoho organization,
            # never an upstream message. A list value repeats its parameter (`doseq`).
            suffix += "&" + urllib.parse.urlencode(extra, doseq=True)
        return oauth_flow.return_url(oauth_flow.resolve_return_app(return_app), suffix,
                                     org=org_id)

    def _avertissement(verdict: "zoho_oauth.OrgCheck") -> "dict | None":
        """The warning of a stored consent, and — `zoho_org_unverified` — the Zoho
        organization(s) consented, SHOWN so an admin can copy the ID into the key's
        `zoho_org_id`: `consented_org` / `consented_org_id`, repeated per organization and
        paired by position. Nothing of it is kept. Read failed: the warning alone."""
        if not verdict.warning:
            return None
        extra: dict = {"warning": verdict.warning}
        if verdict.consented:
            extra["consented_org"] = [o.name for o in verdict.consented]
            extra["consented_org_id"] = [o.shown_id for o in verdict.consented]
        return extra

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        erreur = request.query_params.get("error")
        parsed = zoho_oauth.verify_state(state) if state else None
        if not parsed:
            # Unreadable state: neither `return_app` nor `org` (they live inside it), nor the
            # real connector — `_retour` falls back to its default ("zoho").
            return RedirectResponse(_retour("error"),
                                    status_code=302)
        if erreur or not code:
            # The person's refusal comes back WITHOUT a code (`error=access_denied`):
            # `denied`, on the card that asked. Nothing is exchanged nor written.
            logger.info("zoho oauth callback: consent not completed (sub=%s, connector=%s, "
                        "reason=%s)", parsed["sub"], parsed["connector"],
                        erreur or "missing code")
            return RedirectResponse(
                _retour(oauth_flow.etat_sans_code(erreur), parsed["return_app"],
                        parsed["org"], parsed["connector"]),
                status_code=302)

        def _finish() -> "zoho_oauth.OrgCheck":
            # The SAME resolution as the start, from the signed state: org AND team
            # (the dedicated app of a team, the BYO of its cascade), and the REGION —
            # the publisher app is keyed by data center. Re-resolved from the ambient
            # context, the callback (no session) saw the HOME team, not the start's: the
            # code was then exchanged with another app than the one it was issued for.
            if parsed["org"] and not roles.is_org_member(parsed["sub"], parsed["org"]):
                raise zoho_oauth.ZohoOAuthError("no longer a member of the start's org")
            app = zoho_oauth.app_fields(parsed["connector"], parsed["sub"],
                                        parsed["data_center"], org=parsed["org"],
                                        group=parsed["group"])
            tokens = zoho_oauth.exchange_code(code, parsed["data_center"], app=app)
            # BEFORE storing: the consented Zoho organization against the one the app
            # expects. A mismatch revokes the token and raises — nothing is stored.
            verdict = zoho_oauth.check_org(parsed["connector"],
                                           parsed["data_center"], tokens, app)
            zoho_oauth.persist(parsed["sub"], parsed["org"], parsed["connector"],
                               parsed["data_center"], tokens, app=app,
                               level=parsed["level"])
            return verdict

        try:
            # sync DB + HTTP → off the event loop
            verdict = await run_in_threadpool(_finish)
        except zoho_oauth.ZohoOrgMismatch as e:
            logger.warning("zoho oauth callback refused: zoho_org_mismatch")
            return RedirectResponse(
                _retour("zoho_org_mismatch", parsed["return_app"], parsed["org"],
                        parsed["connector"], {"consented_org": e.consented}),
                status_code=302)
        except Exception as e:  # noqa: BLE001
            # Never the detail in the URL (it could carry an upstream message);
            # the diagnosis goes to the log, without secrets (#284).
            logger.warning("zoho oauth callback failed: %s", type(e).__name__)
            return RedirectResponse(
                _retour("error", parsed["return_app"], parsed["org"],
                        parsed["connector"]),
                status_code=302)
        return RedirectResponse(
            _retour("connected", parsed["return_app"], parsed["org"],
                    parsed["connector"], _avertissement(verdict)),
            status_code=302)

    return [Route("/api/zoho/oauth/callback", callback, methods=["GET"])]

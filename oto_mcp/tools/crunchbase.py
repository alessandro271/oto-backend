"""Crunchbase — company / person records, on the lib's connector (`oto.tools.crunchbase`).

The lib carries what is proper to Crunchbase: its private data API
(`www.crunchbase.com/v4/data`, the v4 schema without `user_key`), the login probe, and
what a refusal means. It executes in the HOST's browser
(`oto.tools.common.browser_session`): oto 1 provides that browser and keeps the session.

- The browser is **Browserbase** (`browserbase.run`): each call is a `fetch()` inside an
  ephemeral session of the user's Browserbase **Context**, which holds their logged-in
  Crunchbase session (the per-user credential, `crunchbase` vault).
- The connection is the shared flow of `browser_session` (Live View, REST and MCP): the
  lib probes the login window and hands the confirmed session back through `on_jar`,
  which writes it to the vault.
- A refused session (`SessionRejected`) marks the vault row that served "to reconnect"
  (`connector_health.mark_rejected`): only a person logging in again renews it.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR
from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.common.host_session import SessionRejected
from oto.tools.crunchbase import CrunchbaseClient
from oto.tools.crunchbase.client import LOGIN_URL
from starlette.concurrency import run_in_threadpool

from .. import access, browser_session, browserbase
from ..auth.hooks import current_user_sub_from_token
from ..access import ResolvedCredential
from ..connectors import health as connector_health


async def _verify_session(session_id: str) -> browser_session.Verdict:
    """The lib's login probe, on a live session (`browser_session.sonder`)."""
    def evaluate(page_url, page_function, arg=None):
        return asyncio.run(browserbase.run_session_eval(session_id, page_url,
                                                        page_function, arg))
    return await asyncio.to_thread(CrunchbaseClient.probe, evaluate)


def _finish_login(run, jar: str, on_jar: Callable[[str], None]) -> browser_session.Verdict:
    """The lib finishes the login: it probes `jar` and hands it to `on_jar` (the vault)."""
    return CrunchbaseClient(run, on_jar=on_jar).finish_login(jar)


# Declares Crunchbase as a browser-session connector — feeds the REST (dashboard) AND MCP
# connection flows. At import.
browser_session.register("crunchbase", _verify_session, login_url=LOGIN_URL,
                         finish=_finish_login)


def _err(msg: str, code: int = INVALID_PARAMS) -> McpError:
    return McpError(ErrorData(code=code, message=msg))


def _sub() -> str:
    # An identity failure BUBBLES UP (the seam logs it with its reason, #464): only
    # a call genuinely without a token is "unauthenticated".
    sub = current_user_sub_from_token()
    if not sub:
        raise _err("Auth required — this tool only works over the authenticated HTTP transport.")
    return sub


# Expired session: reconnection is HUMAN (login in the Live View, SSO/captcha/
# 2FA handled by the person) — no automatic renewal possible. The message
# tells the agent so it continues without this source instead of retrying.
SESSION_EXPIREE = (
    "Crunchbase session expired or disconnected: a person must reconnect it "
    "(`crunchbase_connect_start`, login in the Live View) — no automatic "
    "reconnection is possible. Until then, every Crunchbase call will fail: "
    "continue without this source and report it as unreachable. The connector's "
    "card now shows it as \"to reconnect\".")


def _session() -> ResolvedCredential:
    """The user's Crunchbase credential: their Browserbase Context (= their logged-in
    Crunchbase session), resolved from the vault, WITH the row that served it (to mark it
    rejected). Raises an actionable McpError if Crunchbase is not connected. SQL:
    call outside the event loop."""
    try:
        return access.resolve_credential("crunchbase", want="byo")
    except McpError:
        raise _err("Crunchbase not connected. Run `crunchbase_connect_start` to "
                   "log in (once) via the Live View.")


async def _call(read: Callable[[CrunchbaseClient], dict]) -> dict:
    """Run one read of the lib's client in the user's Browserbase session."""
    if not browserbase.is_configured():
        raise _err("Browserbase not configured on the platform side "
                   "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).", code=INTERNAL_ERROR)
    rc = await run_in_threadpool(_session)
    client = CrunchbaseClient(browserbase.run, jar=rc.key)
    try:
        return await run_in_threadpool(read, client)
    except SessionRejected as e:
        # Signals #1070/#1076/#1149/#1163: the expiry only showed up to
        # agents, run after run, for six days. Marking the row that ACTUALLY
        # served makes the card "to reconnect" for the person who can act;
        # reconnecting rewrites the row and clears the mark.
        await run_in_threadpool(connector_health.mark_rejected, rc.entity_type,
                                rc.entity_id, "crunchbase", rc.account, str(e))
        raise _err(SESSION_EXPIREE)
    except browserbase.BrowserbaseError as e:
        raise _err(f"Browserbase execution failed: {e}", code=INTERNAL_ERROR)
    except UpstreamHTTPError as e:
        raise _err(f"Crunchbase returned {e.status_code}: {str(e.body)[:200]}",
                   code=INTERNAL_ERROR)
    except ValueError as e:
        raise _err(str(e))


def register(mcp: FastMCP) -> None:

    # --- Onboarding (Live View) --------------------------------------------
    @mcp.tool()
    def crunchbase_connect_start(ctx: Context) -> dict:
        """Start the Crunchbase connection. Opens a remote browser and returns
        a **`live_view_url`**: open it, log in to Crunchbase as usual
        (email/password, SSO, captcha — you handle everything in that window). Then
        call `crunchbase_connect_status(context_id, session_id)` with the returned
        values to finalize (your session is saved; only redo this
        when it expires).
        """
        sub = _sub()
        try:
            out = browser_session.start(sub, "crunchbase")
        except browser_session.SessionError as e:
            raise _err(str(e), code=INTERNAL_ERROR)
        out["instructions"] = ("Open `live_view_url`, log in to Crunchbase, then "
                               "call `crunchbase_connect_status` with context_id + session_id.")
        return out

    @mcp.tool()
    async def crunchbase_connect_status(ctx: Context, context_id: str,
                                        session_id: str) -> dict:
        """Finalize the Crunchbase connection. Checks that you actually logged in
        in the Live View (by calling the private API from your session); if so,
        **saves** your session (the Context) for subsequent calls. Returns
        `{connected, reason}`. `connected=false` with `retry=true`: not logged in yet,
        call it again; `retry=false`: calling again cannot help (`hint` says why)."""
        sub = _sub()
        try:
            res = await browser_session.finalize(sub, "crunchbase", context_id, session_id)
        except browser_session.SessionError as e:
            raise _err(str(e), code=INTERNAL_ERROR)
        if not res.connected:
            return {"connected": False, "reason": res.reason, "retry": res.retry,
                    "hint": res.detail or "Not logged in yet — log in in the Live "
                                          "View then retry."}
        out = {"connected": True, "context_id": context_id, "reason": res.reason,
               "login_verified": not res.warning}
        if res.warning:
            out["warning"], out["retry"] = res.warning, False
        return out

    # --- Lecture ------------------------------------------------------------
    @mcp.tool()
    async def crunchbase_get_company(slug: str) -> dict:
        """Crunchbase company record by permalink (slug).

        Returns the raw `/v4/data` response: `properties` (firmographics: name,
        description, founding, location, headcount, funding…) + `cards`
        (`founders`, `raised_funding_rounds`). Structured data, to use as
        is.

        Args:
            slug: the organization's permalink (e.g. "anthropic" from
                `crunchbase.com/organization/anthropic`) — a full URL is also
                accepted (the slug is extracted from it).
        """
        return await _call(lambda c: c.get_company(slug))

    @mcp.tool()
    async def crunchbase_get_person(slug: str) -> dict:
        """Crunchbase person record by permalink (slug).

        Returns the raw `/v4/data` response: the person's `properties` (name, bio,
        social links…).

        Args:
            slug: permalink from `crunchbase.com/person/<slug>` (full URL
                accepted).
        """
        return await _call(lambda c: c.get_person(slug))

    @mcp.tool()
    async def crunchbase_search_companies(query: str, limit: int = 10) -> dict:
        """Search organizations by free text (Crunchbase autocomplete).

        Returns `{entities}` (raw): each entry carries an `identifier` with
        `permalink` (→ slug for `crunchbase_get_company`), `value` (name) and
        `entity_def_id`. `limit` from 1 to 25.
        """
        return await _call(lambda c: c.search_companies(query, limit))

    @mcp.tool()
    async def crunchbase_search_people(query: str, limit: int = 10) -> dict:
        """Search people by free text (Crunchbase autocomplete).

        Returns `{entities}` (raw): each entry carries an `identifier` with
        `permalink` (→ slug for `crunchbase_get_person`), `value` (name) and
        `entity_def_id`. `limit` from 1 to 25.
        """
        return await _call(lambda c: c.search_people(query, limit))

    @mcp.tool()
    async def crunchbase_get_funding_rounds(slug: str) -> dict:
        """Funding rounds of an organization.

        Returns the raw `raised_funding_rounds` card (date, type, amount,
        investors) from `/v4/data`.

        Args:
            slug: the organization's permalink (full URL accepted).
        """
        return await _call(lambda c: c.get_funding_rounds(slug))

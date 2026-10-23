"""Handler of the DURABLE resume link of the `wttj` connector —
`GET /api/wttj/resume/{token}`.

WTTJ's own `resume_url` is signed for 30 minutes; the link the tool hands out
(`resume_link`) is not. On each click this route asks WTTJ for the candidate again and
redirects (302) to the fresh `resume_url`. Nothing is stored, no resume content goes
through oto: the browser downloads from WTTJ's CDN.

**No JWT**: the token of the address is the authority (`typ="wttj_resume"`, sealed
account / org / team / candidate, 30 days, NOT single use). Checks, cheapest first:

1. signature, `typ`, expiry (`wttj_resume.verify`);
2. identity guard of the sealed account (`garde_identite.refus`);
3. sealed org and team REPLAYED: membership, suspension, activation, the WTTJ key
   resolved under that account/org/team (`wttj_resume.prepare`, off the loop);
4. `get_candidate`, then 302 to the fresh `resume_url` (`wttj_resume.fresh_resume_url`,
   off the loop, no database).

Refusals are short JSON bodies a human can read (`detail`), with no internal id and no
name. One `tool_calls` row per opening past the signature (background task, like the
Pennylane relay).
"""
from __future__ import annotations

import asyncio
import logging
import time

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .. import db, garde_identite
from ..capabilities._types import ContratDeRoute, DeclaredError
from ..tools import wttj_resume
from .base import _json_error

logger = logging.getLogger(__name__)

_JOURNAL_TASKS: set = set()  # references to the journal tasks (anti-GC)


async def _open(payload: dict, trace: dict) -> str:
    sub = payload["sub"]
    if (cut := await run_in_threadpool(garde_identite.refus, sub)):
        raise wttj_resume.Refused(403, cut[0],
                                  "The account that created this link can no longer "
                                  "use it.")
    target = payload.get("target") or {}
    prep = await run_in_threadpool(wttj_resume.prepare, sub, payload.get("org"),
                                   target.get("group"))
    trace["key_mode"] = prep.key_mode
    return await run_in_threadpool(wttj_resume.fresh_resume_url, prep,
                                   target["candidate_reference"])


async def _write_journal(row: dict) -> None:
    try:
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:  # noqa: BLE001 — the journal never breaks the opening
        logger.warning("wttj resume link: journal write failed", exc_info=True)


def _journal(payload: dict, trace: dict, start: float, *, ok: bool, error) -> None:
    row = wttj_resume.journal_line(
        payload, ok=ok, error=error, duration_ms=int((time.monotonic() - start) * 1000),
        key_mode=trace.get("key_mode"))
    task = asyncio.create_task(_write_journal(row))
    _JOURNAL_TASKS.add(task)
    task.add_done_callback(_JOURNAL_TASKS.discard)


async def resume_open(request: Request):
    """Redirects to the candidate's resume, freshly signed by WTTJ."""
    payload = wttj_resume.verify(request.path_params.get("token", ""))
    if payload is None:
        return _json_error(request, 401, "invalid_or_expired_token",
                           "This resume link is unreadable, altered or expired: ask "
                           "the agent for the candidate again to get a new one.")
    start, trace = time.monotonic(), {}
    try:
        url = await _open(payload, trace)
    except wttj_resume.Refused as e:
        _journal(payload, trace, start, ok=False, error=e.code)
        return _json_error(request, e.status, e.code, e.message)
    except Exception as e:
        _journal(payload, trace, start, ok=False, error=type(e).__name__)
        raise
    _journal(payload, trace, start, ok=True, error=None)
    # `no-store`: the target expires in 30 minutes, a cached redirect would outlive it.
    return RedirectResponse(url, status_code=302,
                            headers={"Cache-Control": "no-store",
                                     "Referrer-Policy": "no-referrer"})


# ── The published contract (oto#106) ─────────────────────────────────────────────

resume_open.contrat = ContratDeRoute(
    description=(
        "Opens a candidate's resume from Welcome to the Jungle through the durable link "
        "the agent tool `wttj_candidate` returns as `resume_link`. No `Authorization` "
        "header: the link's token seals the account, the organization, the team and "
        "the candidate, lives 30 days and can be opened again. On each opening the "
        "server replays the account's membership and the connector's key, asks WTTJ "
        "for the candidate, and answers **302** to the resume URL WTTJ has just signed "
        "(valid 30 minutes). Nothing is stored and no resume content goes through the "
        "server."),
    errors=(
        DeclaredError(401, "invalid_or_expired_token",
                      "the address token is unreadable, forged, expired, or not a "
                      "resume-link token: get the candidate again for a new link"),
        DeclaredError(403, "no_organization", "the link has no sealed organization"),
        DeclaredError(403, "not_an_org_member",
                      "the account that created the link no longer belongs to the "
                      "sealed organization"),
        DeclaredError(403, "not_a_team_member",
                      "the account that created the link no longer belongs to the "
                      "sealed team"),
        DeclaredError(403, "org_suspended", "the sealed organization is suspended"),
        DeclaredError(403, "connector_disabled",
                      "the connector is disabled for the sealed organization or team"),
        DeclaredError(424, "credential_unavailable",
                      "no WTTJ key is reachable any more for the sealed account, "
                      "organization and team"),
        DeclaredError(404, "candidate_not_found", "WTTJ no longer finds the candidate"),
        DeclaredError(404, "no_resume", "the candidate has no resume on file"),
        DeclaredError(424, "wttj_access_refused",
                      "WTTJ refused the key for this candidate (401/403)"),
        DeclaredError(503, "wttj_rate_limited", "WTTJ asked to slow down (429)"),
        DeclaredError(502, "wttj_unavailable",
                      "WTTJ answered with an error, or with an unusable resume address"),
        DeclaredError(502, "wttj_unreachable", "WTTJ did not answer"),
    ),
    authentifiee=False,
)


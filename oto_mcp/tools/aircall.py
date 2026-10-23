"""Aircall — cloud telephony, READ-ONLY.

Wraps `oto.tools.aircall.AircallClient` (Public API, Basic `api_id:api_token`),
credential resolved per call via `access.resolve_credential_fields("aircall")`
(ADR 0011). No writes, no webhooks.

**Surface**:
- `aircall_calls` (list/search/get/recording/voicemail) — the call log, a
  call (never its recording or voicemail links: the client removes them), and
  the audio of a call, downloaded by the client (`get_call_recording`,
  `get_call_voicemail`) and put in transit (`transit.deposer`): a short
  `source` `{kind: "transit", ref}` read server-side by
  `transcription_create`;
- `aircall_call_ai` — a call's conversational AI (transcription, summary,
  topics, sentiment, action items), served by upstream only to companies
  subscribed to its AI offer;
- `aircall_users`, `aircall_teams`, `aircall_numbers`, `aircall_contacts`,
  `aircall_company` — the company directory.

Lists return a tightened view by default (denylist of named keys, at any
depth: API links redundant with the id, the number's audio files,
deprecated fields); `full=True` returns the raw data, and the response NAMES what it
removed.

Calls to the client are written out in full (`_client().list_calls(…)`): this is what
makes them verifiable by the version-skew probe
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, transit
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

_NAME = "aircall"

# Keys removed from lists by default, at any depth. `direct_link` repeats
# the id; `messages` = the URLs of a number's audio files (greeting, hold…);
# the rest is deprecated upstream or does not help choose what to open.
_DROP_CALLS = frozenset({
    "direct_link", "messages", "cost", "ivr_options_selected", "ai_voice_agents",
    "is_ivr", "open", "availability_status", "available", "substatus",
    "wrap_up_time", "priority", "live_recording_activated",
})
_DROP_DIRECTORY = frozenset({"direct_link", "messages", "is_ivr"})
_HINT = "full=true returns the raw records."

_AI_KINDS = ("transcription", "summary", "topics", "sentiments", "action_items")

Bound = Union[int, str, None]


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _slim(value: Any, drop: frozenset, dropped: set) -> Any:
    """Copy of `value` without the keys in `drop`, at any depth."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in drop:
                dropped.add(k)
                continue
            out[k] = _slim(v, drop, dropped)
        return out
    if isinstance(value, list):
        return [_slim(v, drop, dropped) for v in value]
    return value


def _view(payload: Any, full: bool, drop: frozenset) -> Any:
    """Tightened view of a list (unless `full`); `meta` (pagination) untouched."""
    if full or not isinstance(payload, dict):
        return payload
    dropped: set = set()
    out = {k: (v if k == "meta" else _slim(v, drop, dropped))
           for k, v in payload.items()}
    if dropped:
        out["projection"] = {"omitted": sorted(dropped), "hint": _HINT}
    return out


def _refuse_ignored(op: str, **provided) -> None:
    """An argument supplied that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op='{op}' does not use `{name}`.")


def _need(value, name: str, op: str):
    if value is None or value == "":
        raise _bad(f"op='{op}' requires `{name}`.")
    return value


def _upstream_message(e, *, ai_kind: Optional[str] = None) -> str:
    status = e.status_code
    if status in (401, 403):
        base = (f"Aircall refused access (HTTP {status}): the API ID / API token "
                "pair is invalid or revoked, or the company is inactive or not "
                "verified.")
        if ai_kind:
            base += (" Conversation intelligence is served only to companies on "
                     "the Aircall AI package (AI Assist or AI Assist Pro).")
        return base
    if status == 404:
        if ai_kind:
            return (f"Aircall: no {ai_kind} for this call (404). The call may not "
                    "have been analysed (too short, not recorded, still "
                    "processing), or the company is not on the Aircall AI "
                    "package (AI Assist or AI Assist Pro; `realtime` "
                    "transcription needs AI Assist Pro).")
        return ("Aircall: not found (404). Calls older than six months are not "
                "served, nor contacts synced from third-party integrations.")
    if status == 429:
        return ("Aircall: rate limit reached (429) — 120 requests per minute per "
                "company. Retry in a minute.")
    if status >= 500:
        return f"Aircall is temporarily unavailable (HTTP {status}); retry later."
    return f"Aircall refused the request (HTTP {status}): {str(e.body)[:400]}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Probe for "test the connection": `GET /v1/ping`, the cheapest authenticated
    call. An Aircall key gives access to the whole company, there is no
    finer scope to test."""
    from oto.tools.aircall import AircallClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        AircallClient(api_id=fields.get("api_id"),
                      api_token=fields.get("api_token")).probe()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Aircall HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Aircall HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    import requests
    from oto.connectors.files import FileOutputError
    from oto.tools.aircall import AircallClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> AircallClient:
        creds = access.resolve_credential_fields(_NAME)
        return AircallClient(api_id=creds.get("api_id"),
                             api_token=creds.get("api_token"))

    def _run(fn, *, ai_kind: Optional[str] = None):
        try:
            return fn()
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, ai_kind=ai_kind))
        except ValueError as e:
            raise _bad(str(e))

    def _audio(op: str, call_id: int) -> dict:
        """The `recording` or `voicemail` of a call, downloaded by the client (50 MB,
        120 s, no redirect followed) and put in transit under the caller."""
        try:
            f = _run(lambda: _client().get_call_recording(call_id) if op == "recording"
                     else _client().get_call_voicemail(call_id))
            return transit.deposer("aircall-recordings", f.content, f.mime, f.name)
        except FileOutputError as e:
            raise _bad(f"Aircall {op}: {e}") from None
        except requests.Timeout:
            raise _bad(f"Aircall {op}: the audio was not downloaded within 120 s; "
                       "retry.") from None
        except transit.TransitError as e:
            raise _bad(f"Aircall {op}: {e}") from None

    @mcp.tool()
    def aircall_calls(
        op: Literal["list", "search", "get", "recording", "voicemail"] = "list",
        call_id: Optional[int] = None,
        date_from: Bound = None,
        date_to: Bound = None,
        order: Optional[Literal["asc", "desc"]] = None,
        direction: Optional[Literal["inbound", "outbound"]] = None,
        user_id: Optional[int] = None,
        phone_number: Optional[str] = None,
        fetch_contact: bool = False,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Aircall calls of the company: who called whom, when, how long, the
        line, the agent, tags and comments — and the recording or voicemail.

        `op`:
        - `list` — calls bounded on their creation date (`date_from`/`date_to`).
          Ascending by default: pass `order="desc"` for the latest first.
        - `search` — same, filtered by `direction`, `user_id` (agent, from
          `aircall_users`) or `phone_number` (the external number, E.164). A
          call transferred from line A to line B is found by B only.
        - `get` — one call (`call_id`), raw.
        - `recording` / `voicemail` — the call's audio, kept a day: pass the
          returned `source` as is to `transcription_create`, never the mp3 URL.

        Six months of history only, and at most 10,000 calls through pagination:
        narrow with `date_from` to reach further. Pagination: `page` from 1,
        `per_page` 1-50 (default 20); `meta.next_page_link` is null on the last
        page and `meta.total` counts all matches.

        A call never carries its recording or voicemail links: the audio comes
        through `op="recording"` / `op="voicemail"` only. `duration` includes
        ringing; talk time = `ended_at - answered_at`. Times are UNIX seconds,
        UTC. Inbound missed calls have `answered_at` null and a
        `missed_call_reason`.

        Lists drop by default the API links, the line's audio files and fields
        deprecated upstream (named under `projection`); `full=true` returns raw
        records.

        Args:
            op: list | search | get | recording | voicemail.
            call_id: op='get' / 'recording' / 'voicemail' — the call id.
            date_from: lower bound on creation date — UNIX seconds, ISO date
                (2026-09-01 = midnight UTC) or ISO date-time with offset.
            date_to: upper bound on creation date, same formats.
            order: asc (default) | desc, by creation date.
            direction: op='search' — inbound | outbound.
            user_id: op='search' — the agent who made or received the calls.
            phone_number: op='search' — the external number of the calls.
            fetch_contact: adds the contact details of each call.
            page: page number, from 1.
            per_page: calls per page, 1-50 (default 20).
            full: list/search — raw records instead of the trimmed view.
        """
        flags = dict(fetch_contact=fetch_contact or None)
        if op in ("get", "recording", "voicemail"):
            _need(call_id, "call_id", op)
            _refuse_ignored(op, date_from=date_from, date_to=date_to, order=order,
                            direction=direction, user_id=user_id,
                            phone_number=phone_number, page=page,
                            per_page=per_page)
            if op != "get":
                _refuse_ignored(op, fetch_contact=fetch_contact, full=full)
                return _audio(op, call_id)
            return _run(lambda: _client().get_call(call_id, **flags))
        _refuse_ignored(op, call_id=call_id)
        if op == "list":
            _refuse_ignored(op, direction=direction, user_id=user_id,
                            phone_number=phone_number)
            return _view(_run(lambda: _client().list_calls(
                date_from=date_from, date_to=date_to, order=order, page=page,
                per_page=per_page, **flags)), full, _DROP_CALLS)
        if op == "search":
            return _view(_run(lambda: _client().search_calls(
                date_from=date_from, date_to=date_to, order=order,
                direction=direction, user_id=user_id, phone_number=phone_number,
                page=page, per_page=per_page, **flags)), full, _DROP_CALLS)
        raise _bad(f"invalid `op`: {op!r} (expected: list | search | get | recording "
                   "| voicemail).")

    @mcp.tool()
    def aircall_call_ai(
        call_id: int,
        op: Literal["transcription", "summary", "topics", "sentiments",
                    "action_items"] = "transcription",
        mode: Optional[Literal["async", "realtime"]] = None,
    ) -> Any:
        """Aircall conversation intelligence of one call: what was SAID and what
        came out of it.

        `op`:
        - `transcription` — utterances in order, each with `participant_type`
          (internal = agent, with `user_id`; external = caller, with
          `phone_number`) and `start_time`/`end_time` in seconds from the start
          of the call; `content.language` gives the language. `mode`: `async`
          (post-call transcription) or `realtime` (the one shown to the agent
          during the call, AI Assist Pro).
        - `summary` — the AI summary (text).
        - `topics` — key topics (list of strings).
        - `sentiments` — POSITIVE / NEUTRAL / NEGATIVE per participant.
        - `action_items` — follow-ups; `ai_generated` false means written by an
          agent.

        Served only to companies on the Aircall AI package (AI Assist or AI
        Assist Pro), and only for calls that were analysed: a 404 means no such
        content for this call. Call metadata (who, when, recording) is not here:
        `aircall_calls` op='get'.

        Args:
            call_id: the Aircall call id.
            op: transcription | summary | topics | sentiments | action_items.
            mode: op='transcription' — async | realtime.
        """
        if op not in _AI_KINDS:
            raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(_AI_KINDS)}).")
        if op != "transcription":
            _refuse_ignored(op, mode=mode)
        label = op.replace("_", " ")
        if op == "transcription":
            return _run(lambda: _client().get_transcription(call_id, mode=mode),
                        ai_kind=label)
        if op == "summary":
            return _run(lambda: _client().get_summary(call_id), ai_kind=label)
        if op == "topics":
            return _run(lambda: _client().get_topics(call_id), ai_kind=label)
        if op == "sentiments":
            return _run(lambda: _client().get_sentiments(call_id), ai_kind=label)
        return _run(lambda: _client().get_action_items(call_id), ai_kind=label)

    @mcp.tool()
    def aircall_users(
        op: Literal["list", "get"] = "list",
        user: Optional[str] = None,
        date_from: Bound = None,
        date_to: Bound = None,
        order: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Aircall users (agents) of the company: id, name, email, availability,
        time zone, extension. The `id` is what `aircall_calls` op='search'
        takes as `user_id`.

        `op`: `list` (bounded on creation date, paginated: `page` from 1,
        `per_page` 1-50, default 20) | `get` (`user` = numeric id or email).

        Args:
            op: list | get.
            user: op='get' — the user's id or email address.
            date_from: op='list' — lower bound on creation date (UNIX seconds or ISO).
            date_to: op='list' — upper bound on creation date.
            order: op='list' — asc (default) | desc, by creation date.
            page: op='list' — page number, from 1.
            per_page: op='list' — users per page, 1-50 (default 20).
            full: op='list' — raw records instead of the trimmed view.
        """
        if op == "get":
            _need(user, "user", op)
            _refuse_ignored(op, date_from=date_from, date_to=date_to, order=order,
                            page=page, per_page=per_page)
            return _run(lambda: _client().get_user(user))
        if op == "list":
            _refuse_ignored(op, user=user)
            return _view(_run(lambda: _client().list_users(
                date_from=date_from, date_to=date_to, order=order, page=page,
                per_page=per_page)), full, _DROP_DIRECTORY)
        raise _bad(f"invalid `op`: {op!r} (expected: list | get).")

    @mcp.tool()
    def aircall_teams(
        op: Literal["list", "get"] = "list",
        team_id: Optional[int] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Aircall teams (groups of users used to route inbound calls), each with
        its users.

        `op`: `list` (paginated: `page` from 1, `per_page` 1-50, default 20) |
        `get` (`team_id`).

        Args:
            op: list | get.
            team_id: op='get' — the team id.
            order: op='list' — asc (default) | desc, by creation date.
            page: op='list' — page number, from 1.
            per_page: op='list' — teams per page, 1-50 (default 20).
            full: op='list' — raw records instead of the trimmed view.
        """
        if op == "get":
            _need(team_id, "team_id", op)
            _refuse_ignored(op, order=order, page=page, per_page=per_page)
            return _run(lambda: _client().get_team(team_id))
        if op == "list":
            _refuse_ignored(op, team_id=team_id)
            return _view(_run(lambda: _client().list_teams(
                order=order, page=page, per_page=per_page)), full, _DROP_DIRECTORY)
        raise _bad(f"invalid `op`: {op!r} (expected: list | get).")

    @mcp.tool()
    def aircall_numbers(
        op: Literal["list", "get"] = "list",
        number_id: Optional[int] = None,
        date_from: Bound = None,
        date_to: Bound = None,
        order: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Aircall phone numbers (lines) of the company: digits, name, country,
        time zone, whether live recording is on, and the users on the line.

        `op`: `list` (bounded on creation date, paginated: `page` from 1,
        `per_page` 1-50, default 20) | `get` (`number_id`). The list drops the
        line's audio-file URLs and deprecated fields; `full=true` keeps them.

        Args:
            op: list | get.
            number_id: op='get' — the number id.
            date_from: op='list' — lower bound on creation date (UNIX seconds or ISO).
            date_to: op='list' — upper bound on creation date.
            order: op='list' — asc (default) | desc, by creation date.
            page: op='list' — page number, from 1.
            per_page: op='list' — numbers per page, 1-50 (default 20).
            full: op='list' — raw records instead of the trimmed view.
        """
        if op == "get":
            _need(number_id, "number_id", op)
            _refuse_ignored(op, date_from=date_from, date_to=date_to, order=order,
                            page=page, per_page=per_page)
            return _run(lambda: _client().get_number(number_id))
        if op == "list":
            _refuse_ignored(op, number_id=number_id)
            return _view(_run(lambda: _client().list_numbers(
                date_from=date_from, date_to=date_to, order=order, page=page,
                per_page=per_page)), full, _DROP_DIRECTORY)
        raise _bad(f"invalid `op`: {op!r} (expected: list | get).")

    @mcp.tool()
    def aircall_contacts(
        op: Literal["list", "search", "get"] = "list",
        contact_id: Optional[int] = None,
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
        date_from: Bound = None,
        date_to: Bound = None,
        order: Optional[Literal["asc", "desc"]] = None,
        order_by: Optional[Literal["created_at", "updated_at"]] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Aircall shared contacts of the company: name, company, phone numbers,
        emails. Contacts synced from third-party integrations (CRM…) are NOT
        served by the API, even though agents see them in Aircall.

        `op`:
        - `list` — bounded on creation date, sorted by `order_by`.
        - `search` — by `phone_number` and/or `email`.
        - `get` — one contact (`contact_id`).

        At most 10,000 contacts through pagination (`page` from 1, `per_page`
        1-50, default 20); narrow with `date_from` beyond that.

        Args:
            op: list | search | get.
            contact_id: op='get' — the contact id.
            phone_number: op='search' — a phone number of the contact.
            email: op='search' — an email address of the contact.
            date_from: lower bound on creation date (UNIX seconds or ISO).
            date_to: upper bound on creation date.
            order: asc (default) | desc.
            order_by: created_at (default) | updated_at.
            page: page number, from 1.
            per_page: contacts per page, 1-50 (default 20).
            full: list/search — raw records instead of the trimmed view.
        """
        if op == "get":
            _need(contact_id, "contact_id", op)
            _refuse_ignored(op, phone_number=phone_number, email=email,
                            date_from=date_from, date_to=date_to, order=order,
                            order_by=order_by, page=page, per_page=per_page)
            return _run(lambda: _client().get_contact(contact_id))
        _refuse_ignored(op, contact_id=contact_id)
        if op == "list":
            _refuse_ignored(op, phone_number=phone_number, email=email)
            return _view(_run(lambda: _client().list_contacts(
                date_from=date_from, date_to=date_to, order=order,
                order_by=order_by, page=page, per_page=per_page)),
                full, _DROP_DIRECTORY)
        if op == "search":
            if not (phone_number or email):
                raise _bad("op='search' requires `phone_number` or `email`.")
            return _view(_run(lambda: _client().search_contacts(
                phone_number=phone_number, email=email, date_from=date_from,
                date_to=date_to, order=order, order_by=order_by, page=page,
                per_page=per_page)), full, _DROP_DIRECTORY)
        raise _bad(f"invalid `op`: {op!r} (expected: list | search | get).")

    @mcp.tool()
    def aircall_company() -> Any:
        """The Aircall company behind the connected API key: its name, number of
        users and number of phone numbers."""
        return _run(lambda: _client().get_company())

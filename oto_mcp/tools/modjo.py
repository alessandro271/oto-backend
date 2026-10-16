"""Modjo — conversation intelligence: recorded sales calls and what Modjo drew
from them.

Wraps `oto.tools.modjo.ModjoClient` (Public API v2, `Authorization: Bearer`).
keyed `api_key`, **byo-only**: a key belongs to one Modjo workspace and sees
EVERY member's calls.

29 library functions plus `ask_agent` over FIVE tools, verb in `op`:
`modjo_calls` here; `modjo_reviews`, `modjo_crm`, `modjo_workspace` and
`modjo_ask` in `modjo_annuaire.py`. The shared base (key, refusal, pagination)
lives in `modjo_socle.py`.

## What the tool layer adds to the transport

1. **A list is an index** (ADR 0047): `op="list"` only expands light relations
   (participants, deal, account, users, libraries, tags, AI scoring); summaries,
   notes, next steps, topics and CRM answers are expanded one call at a time
   (`op="get"`), the transcript only through `op="transcript"`.
2. **The transcript is served as readable lines, by slices** of 30,000
   characters (`offset` / `next_offset`), like Claap; `full=True` returns the
   timed JSON blocks, refused beyond one slice.
3. **The import is a PREVIEW until `confirm=True`**: the API cannot delete a
   call, and Modjo logs it in the connected CRM. The preview checks that the
   `user` participants are workspace members (the import is refused otherwise)
   and sends nothing.
4. **No argument silently ignored**: an argument an `op` does not use is
   refused.

Client calls are written in plain sight (`_client().list_calls(…)`) for the
version probe (`test_tools_client_methods_exist`). Checked against the library's
contract (`connectors/modjo/connector.yaml`, Modjo's public OpenAPI v2);
**not tested live** — no Modjo key available at writing time.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from .modjo_socle import _bad, _client, _verify, hors_op, instant_utc, need, paged, run

#: A transcript is served by slices of at most this many characters.
TRANSCRIPT_MAX_CHARS = 30_000

#: Relations `op="list"` may inline: light, bounded per call.
LIST_EXPANDS = ("contacts", "deal", "account", "users", "libraries", "tags", "ai-scoring")
#: `op="get"` adds the sub-resources of ONE call. The transcript is never here:
#: it is served by slices, through `op="transcript"`.
GET_EXPANDS = LIST_EXPANDS + ("topics", "summaries", "notes", "next-steps",
                              "crm-filling-answers")

CALL_NOT_FOUND = "no call with this id in the Modjo workspace"

_FILTERS = ("ids", "title", "min_duration", "date_from", "date_to", "created_from",
            "created_to", "user_id", "deal_id", "account_id",
            "recording_retention_status")
_UPLOAD = ("media_url", "date", "participants", "name", "direction", "duration",
           "tags", "account", "deal", "crm_activity_id", "dry_run", "confirm")
_PERMIS = {
    "list": _FILTERS + ("expand", "page", "size"),
    "get": ("call_id", "expand"),
    "transcript": ("call_id", "offset", "full"),
    "summaries": ("call_id",), "notes": ("call_id",), "next_steps": ("call_id",),
    "recording": ("call_id",), "tags": ("call_id",),
    "crm_answers": ("call_id", "page", "size"),
    "add_tag": ("call_id", "tag_id"), "remove_tag": ("call_id", "tag_id"),
    "upload": _UPLOAD,
}


def _expand(op: str, expand: Optional[List[str]], allowed: tuple) -> Optional[List[str]]:
    for value in expand or []:
        if value not in allowed:
            hint = (" — the transcript is read with op='transcript', by slices"
                    if value == "transcript" else
                    " — expand it on one call, with op='get'" if value in GET_EXPANDS else "")
            raise _bad(f"op={op!r} does not expand {value!r}{hint}. Accepted: "
                       + ", ".join(allowed))
    return expand


def _mmss(seconds: Any) -> str:
    s = int(seconds or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _transcript_text(blocks: List[Dict[str, Any]]) -> str:
    """`mm:ss speaker (type): text`, one line per block."""
    lines = []
    for b in blocks:
        sp = b.get("speaker") or {}
        who = sp.get("name") or f"speaker {sp.get('id')}"
        lines.append(f"{_mmss(b.get('startTime'))} {who} ({sp.get('type')}): {b.get('content')}")
    return "\n".join(lines)


def _member_check(participants: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which `user` participants are members of the workspace — Modjo refuses the
    import when none is (one `list_users(email=…)` per user participant)."""
    seen = {}
    for p in participants:
        if isinstance(p, dict) and p.get("type") == "user" and p.get("email"):
            res = run(lambda e=p["email"]: _client().list_users(email=e, size=1))
            seen[p["email"]] = bool((res or {}).get("data"))
    return seen


def register(mcp: FastMCP) -> None:
    connector_verify.register("modjo", _verify)

    @mcp.tool()
    def modjo_calls(
        op: Literal["list", "get", "transcript", "summaries", "notes", "next_steps",
                    "recording", "crm_answers", "tags", "add_tag", "remove_tag",
                    "upload"] = "list",
        call_id: Optional[str] = None,
        ids: Optional[List[int]] = None,
        title: Optional[str] = None,
        min_duration: Optional[float] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        user_id: Optional[int] = None,
        deal_id: Optional[int] = None,
        account_id: Optional[int] = None,
        recording_retention_status: Optional[Literal["available", "deleted"]] = None,
        expand: Optional[List[Literal["contacts", "deal", "account", "users", "libraries",
                                      "tags", "ai-scoring", "topics", "summaries", "notes",
                                      "next-steps", "crm-filling-answers"]]] = None,
        page: Optional[int] = None,
        size: Optional[int] = None,
        offset: Optional[int] = None,
        full: bool = False,
        tag_id: Optional[int] = None,
        media_url: Optional[str] = None,
        date: Optional[str] = None,
        participants: Optional[List[Dict[str, Any]]] = None,
        name: Optional[str] = None,
        direction: Optional[Literal["inbound", "outbound"]] = None,
        duration: Optional[float] = None,
        tags: Optional[List[str]] = None,
        account: Optional[Dict[str, str]] = None,
        deal: Optional[Dict[str, str]] = None,
        crm_activity_id: Optional[str] = None,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> Any:
        """Modjo — recorded sales calls: list, read, transcript, AI summaries, notes,
        next steps, recording, CRM answers, tags, import.

        ⚠️ The key belongs to the WORKSPACE: `op="list"` without `user_id` returns
        every member's calls (find a member's id with `modjo_workspace op=users`).
        A call is analysed 5 to 30 minutes after it is recorded (`status`
        processing → completed | failed); until then transcript, summaries and
        next steps are empty.

        `op`:
        - `list` — calls, by page (`pagination.next_page`); related objects are
          ids unless `expand`ed (light relations only).
        - `get` — one call (`call_id`, numeric id or UUID); `expand` may also inline
          summaries, notes, next-steps, topics, crm-filling-answers.
        - `transcript` — readable "mm:ss speaker (type): text" lines, by slices of
          30,000 characters: pass `next_offset` back as `offset`. `full=True` →
          timed JSON blocks.
        - `summaries` (AI summaries, one per template) | `notes` (published) |
          `next_steps` | `tags` | `crm_answers` (CRM fields filled by the AI and
          pushed) — sub-resources of one call.
        - `recording` — signed download URL, valid ONE hour: never store it.
        - `add_tag` / `remove_tag` — `tag_id` from `modjo_workspace op=tags`.
        - `upload` — import a call Modjo downloads from `media_url` (public
          https). ⚠️ Cannot be undone through the API, and Modjo logs it in the
          connected CRM: without `confirm=True` it only previews. At least one
          `user` participant must be a licensed member.

        Args:
            op: see above.
            call_id: every op but list and upload — numeric call id or UUID.
            ids: list — only these numeric call ids (100 at most).
            title: list — case-insensitive substring of the title.
            min_duration: list — minimum duration, seconds.
            date_from: list — calls that took place from this ISO 8601 date-time
                (with `Z` or an offset, sent in UTC; a date alone = midnight UTC).
            date_to: list — … until this date-time.
            created_from: list — created in Modjo from this date-time.
            created_to: list — … until this date-time.
            user_id: list — calls of this member.
            deal_id: list — calls linked to this deal.
            account_id: list — calls linked to this account.
            recording_retention_status: list — "deleted" = recording removed by retention.
            expand: list / get — relations to inline (see op).
            page: list / crm_answers — page number, from 1.
            size: list / crm_answers — items per page, 1-100 (default 25).
            offset: transcript — character offset, from `next_offset`.
            full: transcript — timed JSON blocks instead of text.
            tag_id: add_tag / remove_tag — the tag id.
            media_url: upload — REQUIRED, public https URL of the audio or video.
            date: upload — REQUIRED, start of the call, ISO 8601 date-time with `Z` or an offset.
            participants: upload — REQUIRED, [{"email", "type": "user"|"contact", "name"?}].
            name: upload — call title (generated from participants when omitted).
            direction: upload — inbound | outbound (default outbound).
            duration: upload — seconds.
            tags: upload — tag NAMES to set.
            account: upload — CRM account to link: {"crm": "hubspot", "crmId": "…"}.
            deal: upload — CRM deal to link: {"crm": "hubspot", "crmId": "…"}.
            crm_activity_id: upload — id of the call activity already in the CRM, updated instead of created.
            dry_run: upload — preview only, even with `confirm=True`.
            confirm: upload — True to import (after the preview).
        """
        fournis = dict(
            call_id=call_id, ids=ids, title=title, min_duration=min_duration,
            date_from=date_from, date_to=date_to, created_from=created_from,
            created_to=created_to, user_id=user_id, deal_id=deal_id,
            account_id=account_id, recording_retention_status=recording_retention_status,
            expand=expand, page=page, size=size, offset=offset, full=full,
            tag_id=tag_id, media_url=media_url, date=date, participants=participants,
            name=name, direction=direction, duration=duration, tags=tags,
            account=account, deal=deal, crm_activity_id=crm_activity_id,
            dry_run=dry_run, confirm=confirm)
        if op not in _PERMIS:
            raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(_PERMIS)}).")
        hors_op(op, _PERMIS[op], **fournis)

        if op == "list":
            date_from, date_to = (instant_utc(date_from, "date_from"),
                                  instant_utc(date_to, "date_to"))
            created_from, created_to = (instant_utc(created_from, "created_from"),
                                        instant_utc(created_to, "created_to"))
            return paged(run(lambda: _client().list_calls(
                ids=ids, title=title, min_duration=min_duration, date_from=date_from,
                date_to=date_to, created_from=created_from, created_to=created_to,
                user_id=user_id, deal_id=deal_id, account_id=account_id,
                recording_retention_status=recording_retention_status,
                expand=_expand(op, expand, LIST_EXPANDS), page=page, size=size)))

        if op == "upload":
            need(op, media_url=media_url, date=date, participants=participants)
            date = instant_utc(date, "date")
            if dry_run or not confirm:
                members = _member_check(participants)
                return {"dry_run": True, "would_import": {
                    "media_url": media_url, "date": date, "name": name,
                    "direction": direction or "outbound", "participants": participants,
                    "tags": tags, "account": account, "deal": deal,
                    "crm_activity_id": crm_activity_id},
                    "user_participants_found": members,
                    "note": ("Cannot be undone through the API, and Modjo logs the call "
                             "in the connected CRM. "
                             + ("No `user` participant is a workspace member: Modjo will "
                                "refuse the import. " if not any(members.values()) else "")
                             + "Call again with confirm=True to import.")}
            return run(lambda: _client().upload_call(
                media_url, date, participants, name=name, direction=direction,
                duration=duration, tags=tags, account=account, deal=deal,
                crm_activity_id=crm_activity_id))

        need(op, call_id=call_id)
        if op == "get":
            return run(lambda: _client().get_call(
                call_id, expand=_expand(op, expand, GET_EXPANDS)), CALL_NOT_FOUND)
        if op == "transcript":
            if offset is not None and (full or offset < 0):
                raise _bad("`offset` is a character offset (>= 0) of the TEXT "
                           "transcript (without `full`).")
            res = run(lambda: _client().get_call_transcript(call_id), CALL_NOT_FOUND)
            blocks = (res or {}).get("data") or []
            if full:
                poids = len(json.dumps(res, ensure_ascii=False, default=str))
                if poids > TRANSCRIPT_MAX_CHARS:
                    raise _bad(f"the JSON transcript weighs {poids:,} characters (more "
                               f"than {TRANSCRIPT_MAX_CHARS:,} in one result): read the "
                               "text form, served by slices with `offset`.")
                return res
            texte = _transcript_text(blocks)
            debut = offset or 0
            fin = debut + TRANSCRIPT_MAX_CHARS
            tranche = {"call_id": call_id, "transcript": texte[debut:fin],
                       "offset": debut, "total_chars": len(texte), "blocks": len(blocks)}
            if fin < len(texte):
                tranche["next_offset"] = fin
            if not blocks:
                tranche["note"] = ("Empty: the call may still be processing (read "
                                   "`status` with op='get'), or its transcript was "
                                   "deleted by retention.")
            return tranche
        if op == "summaries":
            return run(lambda: _client().list_call_summaries(call_id), CALL_NOT_FOUND)
        if op == "notes":
            return run(lambda: _client().list_call_notes(call_id), CALL_NOT_FOUND)
        if op == "next_steps":
            return run(lambda: _client().list_call_next_steps(call_id), CALL_NOT_FOUND)
        if op == "recording":
            return run(lambda: _client().get_call_recording(call_id),
                       "no recording available for this call, or no such call")
        if op == "tags":
            return run(lambda: _client().list_call_tags(call_id), CALL_NOT_FOUND)
        if op == "crm_answers":
            return paged(run(lambda: _client().list_call_crm_filling_answers(
                call_id, page=page, size=size), CALL_NOT_FOUND))
        need(op, tag_id=tag_id)
        if op == "add_tag":
            return run(lambda: _client().add_call_tag(call_id, tag_id),
                       "no call or no tag with this id in the workspace")
        return run(lambda: _client().remove_call_tag(call_id, tag_id),
                   "this tag is not set on this call, or the call or tag does not exist")

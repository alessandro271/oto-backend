"""Claap tools — meeting recordings, transcripts, recording views.

Wraps `oto.tools.claap.client.ClaapClient` (API v1, `X-Claap-Key` header).
Two tools, one per business object:

- `claap_recordings` — list, read, transcript, create (from a media URL or a
  transcript alone), delete;
- `claap_recording_views` — read saved recording views, so the `view_id` filter
  of `claap_recordings(op="list")` is usable.

Scope is deliberately limited to recordings: the Claap API also exposes deals,
companies, contacts, folders, AI fields, automations and users, not served here.

⚠️ **The key belongs to the workspace, not to a person**: it sees every recording
visible in global search, colleagues' meetings included. "My meetings" =
`recorder_email`. A 404 on `op="get"` can mean "private / not searchable", not
"does not exist" — private recordings are only visible to a "Full access" key.

**No argument is silently dropped**: an `op` that does not use a provided
argument REFUSES it (`_refuse_ignored`), for every op.

Checked against the vendor OpenAPI and **live-tested on 2026-09-29**: list,
filters, cursor, views, detail, transcripts, delete (dry_run and real), errors.
⚠️ Creation could not be verified (trial plan quota reached, 403).

Client calls are written out (`_client().list_recordings(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    """A provided argument THIS op does not use is an intent error — otherwise
    `op="get"` with `recorder_email=` would return a recording while implying the
    filter applied."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


# What a 404 means depends on what was requested — upstream only says "not
# found". A single message for all of them talked about private recordings on a
# missing view or a missing translation (seen live on 2026-09-29).
NOT_FOUND_RECORDING = (
    "⚠️ A private recording, or one in a folder hidden from global search, also "
    "returns 404 to the API (only a \"Full access\" key sees private ones).")
NOT_FOUND_TRANSCRIPT = (
    "With `lang`, this usually means no translation exists in that language: "
    "available languages are in `transcripts[].langIso2` of `op='get'`. "
    + NOT_FOUND_RECORDING)
NOT_FOUND_VIEW = (
    "Only PUBLIC views are visible to the API: a private view returns 404 like a "
    "missing one.")


def _upstream_message(e, not_found: str | None = None) -> str:
    """Turn a Claap refusal into an actionable message.

    Claap's error envelope is `{"error": {type, message, path?}}`: `path` names
    the offending field of a `validation_error`, so it is kept. `not_found` is
    the 404 explanation specific to what was requested.
    """
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    detail = err.get("message") or e.body
    if err.get("path"):
        detail = f"{detail} (field `{err['path']}`)"
    if body.get("recording_id"):
        return (f"Claap: recording {body['recording_id']} was CREATED, but its "
                f"transcript could not be uploaded (HTTP {status}): "
                f"{body.get('error')} {body.get('body') or ''}".strip()
                + " — delete it (`op='delete'`) before retrying, otherwise it "
                  "stays empty.")
    if status == 401:
        return ("Claap rejected the key (401) — check the `cla_…` key set on this "
                "connector (Claap: Settings → API & Webhooks, admin account).")
    if status == 403:
        if "quota" in str(detail).lower():
            return (f"Claap: plan quota reached (403, \"{detail}\") — the workspace "
                    "cannot create more recordings on its current plan. Nothing was "
                    "created; this is fixed on the Claap subscription side.")
        return (f"Claap refused the operation (403) — the workspace plan or the "
                f"author's permissions do not allow it: {detail}")
    if status == 404:
        return f"Claap: not found (404) — {detail}." + (
            f" {not_found}" if not_found else "")
    if status == 429:
        return ("Claap: too many requests (429) — 3/s per endpoint and 3,000/day "
                "for the whole workspace (all keys combined). Retry later.")
    if status in (500, 502, 503, 504):
        return f"Claap is temporarily unavailable (HTTP {status}) — retry later."
    return f"Claap refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: `GET /v1/workspaces/mine`, no parameter, no
    meeting data — a clean 401 if the key is wrong."""
    from oto.tools.claap.client import ClaapClient
    ClaapClient(api_key=fields["key"]).probe()


def _slim_recording(rec: dict) -> dict:
    """Slim view of a listed recording (ADR 0047).

    Drops signed URLs (video, transcripts, thumbnail — they expire after 24 h
    and are heavy), the workspace (always the same) and participant detail
    beyond name/email/attendance.
    """
    meeting = rec.get("meeting") or {}
    recorder = rec.get("recorder") or {}
    out = {
        "id": rec.get("id"),
        "title": rec.get("title"),
        "createdAt": rec.get("createdAt"),
        "durationSeconds": rec.get("durationSeconds"),
        "state": rec.get("state"),
        "source": rec.get("source"),
        "url": rec.get("url"),
        "labels": rec.get("labels"),
        "channel": rec.get("channel"),
        "recorder": {"name": recorder.get("name"), "email": recorder.get("email")},
        "transcriptLanguages": [t.get("langIso2") for t in rec.get("transcripts") or []
                                if t.get("langIso2")],
    }
    if meeting:
        out["meeting"] = {
            "type": meeting.get("type"),
            "startingAt": meeting.get("startingAt"),
            "participants": [
                {k: p.get(k) for k in ("name", "email", "attended") if p.get(k) is not None}
                for p in meeting.get("participants") or []],
        }
    return {k: v for k, v in out.items() if v is not None}


def register(mcp: FastMCP) -> None:
    from oto.tools.claap.client import ClaapClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register("claap", _verify)

    def _client() -> ClaapClient:
        key, _ = access.resolve_api_key("claap")
        return ClaapClient(api_key=key)

    def _run(fn, not_found: str | None = None):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, not_found))

    def _need(value, name: str, op: str):
        if not value:
            raise _bad(f"op={op!r}: `{name}` is required.")
        return value

    # --- recordings ------------------------------------------------------------

    @mcp.tool()
    def claap_recordings(
        op: Literal["list", "get", "transcript", "create", "delete"] = "list",
        recording_id: Optional[str] = None,
        recorder_email: Optional[str] = None,
        recorder_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        channel_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        view_id: Optional[str] = None,
        sort: Optional[Literal["created_asc", "created_desc", "duration_asc",
                               "duration_desc", "title_asc", "title_desc"]] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        full: bool = False,
        format: Optional[Literal["text", "json"]] = None,
        lang: Optional[str] = None,
        author_email: Optional[str] = None,
        title: Optional[str] = None,
        source: Optional[Literal["Aircall", "Allo", "Call", "GoogleMeet",
                                 "LemlistVoip", "Loom", "MsTeams", "Ringover",
                                 "Zoom"]] = None,
        meeting: Optional[dict] = None,
        deal: Optional[dict] = None,
        video_url: Optional[str] = None,
        transcript: Optional[dict] = None,
        dry_run: bool = False,
    ) -> Any:
        """Claap — meeting and call recordings: list, read, transcript, import, delete.

        ⚠️ The key belongs to the WORKSPACE, not a person: `op="list"` returns
        every recording visible in Claap's global search, colleagues' meetings
        included. To get one person's meetings, pass `recorder_email`.

        `op`:
        - `list` — recordings, newest first, cursor-paginated (`pagination.nextCursor`).
          Slim rows by default (no signed URLs); `full=True` for the raw payload.
        - `get` — one recording (`recording_id`): AI summary (`keyTakeaways`,
          `outlines`), `actionItems`, `aiFields`, deal/companies, analytics.
          Check `state`: only `Ready` has the full payload; `Empty`/`Uploaded`/
          `Failed` return a thin shape. Signed URLs in it expire after 24 h.
          A 404 can mean private / not searchable, not only "doesn't exist".
        - `transcript` — the transcript (`recording_id`). `format="text"`
          (default here) → readable "mm:ss speaker: text" lines; `"json"` →
          timed segments with per-word detail (heavy). `lang` picks a translation.
        - `create` — import a recording, created on behalf of `author_email` (a
          workspace member). Either `video_url` (Claap fetches the media: public
          GET, < 2 GiB, < 5 min) or `transcript` alone (no media), or both.
          Processing is async: Empty → Uploaded → Ready | Failed.
        - `delete` — ⚠️ PERMANENT, no undo. `dry_run=True` returns what would be
          deleted (title, recorder, date) without deleting.

        Args:
            op: list | get | transcript | create | delete.
            recording_id: get / transcript / delete — the recording id.
            recorder_email: list — only recordings by this author (email).
            recorder_id: list — only recordings by this author (Claap user id).
            created_after: list — ISO date/datetime lower bound.
            created_before: list — ISO date/datetime upper bound.
            channel_id: list — only this folder; create — folder to put it in.
            labels: list — label names (any match). Requires `channel_id`.
            sources: list — Aircall, Allo, Api, Call, GoogleMeet, LemlistVoip,
                Loom, MobileApp, MsTeams, Ringover, Uploaded, Zoom.
            view_id: list — apply a saved view's filters and sort (from
                `claap_recording_views`); other filters narrow within it.
            sort: list — overrides the view's sort; default created_desc.
            cursor: list — `pagination.nextCursor` from the previous page.
            limit: list — 1-100, default 20.
            full: list — raw recordings instead of slim rows.
            format: transcript — "text" (default) | "json".
            lang: transcript — 2-letter code of a translation.
            author_email: create — REQUIRED, workspace member creating it.
            title: create — recording title.
            source: create — origin platform (default Api).
            meeting: create — {"startedAt": ISO, "endedAt"?: ISO,
                "participants"?: [{"name", "email"?, "isOrganizer"?}]}.
            deal: create — {"type": "hubspot"|"salesforce"|"pipedrive"|"attio",
                "id": CRM deal id}; requires `meeting` and the same CRM
                connected to Claap.
            video_url: create — public URL Claap downloads the media from.
            transcript: create — WRITE shape, different from what `op="transcript"`
                returns: {"segments": [{"start": sec, "end": sec, "speakerId":
                "1", "text": "…"}], "speakers"?: [{"speakerId": "1", "name":
                "…", "email"?, "isRecorder"?}], "langIso2"?: "fr"}.
            dry_run: delete — preview only.
        """
        list_only = dict(
            recorder_email=recorder_email, recorder_id=recorder_id,
            created_after=created_after, created_before=created_before,
            labels=labels, sources=sources, view_id=view_id, sort=sort,
            cursor=cursor, limit=limit, full=full)
        create_only = dict(
            author_email=author_email, title=title, source=source,
            meeting=meeting, deal=deal, video_url=video_url, transcript=transcript)
        transcript_only = dict(format=format, lang=lang)

        if op == "list":
            _refuse_ignored(op, "it does not filter a list",
                            recording_id=recording_id, dry_run=dry_run,
                            **create_only, **transcript_only)
            res = _run(lambda: _client().list_recordings(
                cursor=cursor, limit=limit, sort=sort, channel_id=channel_id,
                labels=labels, sources=sources, recorder_email=recorder_email,
                recorder_id=recorder_id, created_after=created_after,
                created_before=created_before, view_id=view_id))
            if full:
                return res
            result = dict((res or {}).get("result") or {})
            result["recordings"] = [_slim_recording(r)
                                    for r in result.get("recordings") or []]
            return {"result": result}

        if op in ("get", "transcript", "delete"):
            _need(recording_id, "recording_id", op)
            _refuse_ignored(op, "it only applies to op='list' or op='create'",
                            channel_id=channel_id, **list_only, **create_only)
            if op != "transcript":
                _refuse_ignored(op, "`format`/`lang` only apply to op='transcript'",
                                **transcript_only)
            if op != "delete":
                _refuse_ignored(op, "`dry_run` only applies to op='delete'",
                                dry_run=dry_run)

            if op == "get":
                return _run(lambda: _client().get_recording(recording_id),
                            NOT_FOUND_RECORDING)
            if op == "transcript":
                fmt = format or "text"
                out = _run(lambda: _client().get_recording_transcript(
                    recording_id, lang=lang, format=fmt),
                    NOT_FOUND_TRANSCRIPT if lang else NOT_FOUND_RECORDING)
                if fmt == "text":
                    return {"recording_id": recording_id, "format": "text",
                            "lang": lang, "transcript": out}
                return out
            # delete
            if dry_run:
                cur = _run(lambda: _client().get_recording(recording_id),
                           NOT_FOUND_RECORDING)
                rec = ((cur or {}).get("result") or {}).get("recording") or {}
                return {"dry_run": True, "would_delete": {
                    "id": rec.get("id"), "title": rec.get("title"),
                    "createdAt": rec.get("createdAt"), "state": rec.get("state"),
                    "recorder": (rec.get("recorder") or {}).get("email"),
                    "url": rec.get("url")},
                    "note": "PERMANENT deletion on Claap's side — cannot be undone."}
            return _run(lambda: _client().delete_recording(recording_id),
                        NOT_FOUND_RECORDING)

        if op == "create":
            _need(author_email, "author_email", op)
            _refuse_ignored(op, "it only applies to reading",
                            recording_id=recording_id, dry_run=dry_run,
                            **{k: v for k, v in list_only.items()},
                            **transcript_only)
            return _run(lambda: _client().create_recording(
                author_email, title=title, channel_id=channel_id, source=source,
                meeting=meeting, deal=deal, video_url=video_url,
                transcript=transcript))

        raise _bad(f"invalid `op`: {op!r} (expected: list | get | transcript | "
                   "create | delete).")

    # --- recording views -------------------------------------------------------

    @mcp.tool()
    def claap_recording_views(
        op: Literal["list", "get"] = "list",
        view_id: Optional[str] = None,
    ) -> Any:
        """Claap — saved recording views (read-only), to reuse their filters.

        A view is a saved recordings table: filters, sort, columns (including AI
        field columns). Pass its id as `claap_recordings(op="list", view_id=…)`
        to list the recordings it selects.

        Only PUBLIC views are visible to the API; built-in defaults (e.g. "My
        Meetings") come last with `isDefault: true`. A view filtering on `Me`
        matches the whole workspace here, since the key is nobody in particular.

        Args:
            op: list | get.
            view_id: get — the view id.
        """
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific view",
                            view_id=view_id)
            return _run(lambda: _client().list_recording_views())
        if op == "get":
            _need(view_id, "view_id", op)
            return _run(lambda: _client().get_recording_view(view_id),
                        NOT_FOUND_VIEW)
        raise _bad(f"invalid `op`: {op!r} (expected: list | get).")

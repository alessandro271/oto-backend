"""Shared base of the `resend` connector modules.

The connector spans two modules (`Connector.modules` in the registry):
`tools/resend.py` (contacts) and `tools/resend_segments.py` (segments, topics).
This file holds what they have in common — key resolution, translation of a
Resend refusal, argument checks, the trimmed views — so that a fix never
covers only half the connector. It has no `register()`.

**The key.** The org's Resend key, the same one `email_send` (transport=resend)
resolves through `access.resolve_api_key("resend")`. Contacts, segments and
topics need a key with **Full access**; a sending-only key is refused
(401 `restricted_api_key`), and the message says so plainly — and that a
Full-access key also sends, so replacing the key never breaks `email_send`.

No "test connection" probe is registered on purpose: the key may be a
sending-only key that `email_send` uses correctly, and a probe on a contact
route would report it as broken.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError

if TYPE_CHECKING:  # the annotation of `_client()` only — never evaluated
    from oto.tools.resend.client import ResendClient

_CONTACT_KEYS = ("id", "email", "first_name", "last_name", "unsubscribed",
                 "created_at")


def _client() -> ResendClient:
    """The Resend client for THIS caller's key. Real import in the body: tests
    replace the client, and the version probe reads the return annotation."""
    from oto.tools.resend.client import ResendClient

    key, _is_platform = access.resolve_api_key("resend")
    return ResendClient(api_key=key)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def need(value: Any, name: str, op: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()) or value == []:
        raise _bad(f"op={op!r}: `{name}` is required.")
    return value


def bad_op(op: str, expected: str) -> McpError:
    return _bad(f"`op` invalid: {op!r} (expected: {expected}).")


def _detail(e: Any) -> str:
    body = e.body if isinstance(e.body, dict) else {}
    return str(body.get("message") or (e.body if isinstance(e.body, str) else ""))


def key_refused(e: Any) -> bool:
    """A refusal of the KEY itself: every further call would fail the same way."""
    return e.status_code in (401, 403) or "API key" in _detail(e)


def upstream_message(e: Any) -> str:
    """A Resend refusal (`{name, statusCode, message}`), translated into what the
    caller can do about it. Matched on `name` first: the status alone is ambiguous
    (an unknown key is a 400 on these routes, like a malformed field)."""
    status = e.status_code
    name = (e.body.get("name") if isinstance(e.body, dict) else "") or ""
    detail = _detail(e)
    if (status == 401 and name == "restricted_api_key") or "only send emails" in detail:
        return ("This Resend key can only SEND emails (Resend: \"restricted to only send "
                "emails\"). Contacts, segments and topics need a key with Full access: "
                "create one in Resend (API Keys, permission \"Full access\") and set it on "
                "the resend connector. A Full-access key also sends, so email sending "
                "keeps working with it.")
    if name == "missing_api_key" or "API key is invalid" in detail:
        return ("Resend rejected the key (unknown or deleted): check the key set on the "
                "resend connector.")
    if status == 403:
        return f"Resend refused the key (403, {name or 'forbidden'}): {detail}"
    if status == 404:
        return (f"Resend: not found (404). {detail} A contact is addressed by its id or "
                "its email, a segment or a topic by its id.")
    if status == 409:
        return f"Resend: the resource is busy (409), retry in a moment. {detail}"
    if status in (400, 422):
        return f"Resend refused the request ({status}, {name or 'invalid'}): {detail}"
    if status == 429:
        return ("Resend rate limit reached (10 requests per second per team): retry in a "
                "moment; an upsert is safe to run again.")
    if status >= 500:
        return f"Resend is temporarily unavailable (HTTP {status}): retry later."
    return f"Resend refused the request (HTTP {status}): {detail}"


def run(fn: Callable[[], Any]) -> Any:
    """Call the client; a local refusal or a Resend refusal becomes a tool error."""
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e))
    except UpstreamHTTPError as e:
        raise _bad(upstream_message(e))


# --- trimmed views ------------------------------------------------------------


def props(raw: Any) -> dict:
    """`{key: {value, type}}` as Resend returns it → `{key: value}`."""
    return {k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in (raw or {}).items()}


def slim(row: Any) -> Any:
    """Drop the `object` tag and the empty values."""
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in row.items() if k != "object" and v not in (None, "")}


def slim_contact(row: dict) -> dict:
    out = {k: row[k] for k in _CONTACT_KEYS if row.get(k) not in (None, "")}
    if row.get("properties"):
        out["properties"] = props(row["properties"])
    return out


def page_out(key: str, page: Any, full: bool, shape: Callable[[dict], Any]) -> Any:
    """A Resend page `{object, has_more, data}` → `{<key>: rows, has_more,
    next_after}`, `next_after` being the cursor of the next page (the last row's
    id). `full` returns the page untouched."""
    if full:
        return page
    data = (page or {}).get("data") or []
    has_more = bool((page or {}).get("has_more")) and bool(data)
    return {key: [shape(r) for r in data], "has_more": has_more,
            "next_after": data[-1].get("id") if has_more else None}

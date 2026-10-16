"""Shared base of the `modjo` connector modules.

The connector spans two modules (`Connector.modules` in the registry):
`tools/modjo.py` (calls: list, detail, transcript, what Modjo drew from them,
tags, import) and `tools/modjo_annuaire.py` (call reviews, the CRM objects
Modjo mirrors, the workspace directory, the question to Modjo's AI). This file
holds what they have in common — key resolution, translation of a Modjo
refusal, argument checks and the pagination block — so that a fix never covers
only half the connector. It has no `register()`: it is a helper.

**Pagination, said honestly.** Modjo pages by number (`page`, `size` ≤ 100) and
answers `{page, size, total}`, never the next page: every list here adds
`next_page` (null on the last page), computed from `page * size < total`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError

if TYPE_CHECKING:  # the annotation of `_client()` only — never evaluated
    from oto.tools.modjo import ModjoClient

#: Where the user creates their key, in THEIR Modjo workspace.
WHERE_TO_CREATE = ("Modjo → Settings → Integrations → \"Public API\" (an admin or "
                   "a manager creates it)")


def _client() -> ModjoClient:
    """The Modjo client for THIS caller's key (byo: a key belongs to one Modjo
    workspace). Real import in the body: tests replace the client, and the
    version probe reads the return annotation."""
    from oto.tools.modjo import ModjoClient
    key, _is_platform = access.resolve_api_key("modjo")
    return ModjoClient(api_key=key)


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """The "test connection" probe: one tag of the workspace (`GET /tags`), no
    call data and no side effect. A 401 raises `UpstreamHTTPError`, which the
    probe's classification reads by its status."""
    from oto.tools.modjo import ModjoClient
    ModjoClient(api_key=fields["key"]).probe()


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def hors_op(op: str, permis: Iterable[str], **fournis: Any) -> None:
    """An argument that THIS op does not use is an error of intent — otherwise
    `op="get"` with `user_id=` would return one call while letting one believe
    the filter applied. `False` counts as not given (the default of a flag)."""
    permis = set(permis)
    for name, value in fournis.items():
        if value is not None and value is not False and name not in permis:
            raise _bad(f"op={op!r} does not use `{name}`.")


def instant_utc(value: Optional[str], name: str) -> Optional[str]:
    """An ISO 8601 date-time for Modjo, in UTC with `Z`: Modjo refuses an offset
    (`+02:00` → 400) and accepts only `Z`. A date-time with an offset is converted
    (same instant); a date alone is midnight UTC; a date-time WITHOUT offset is
    refused — its time zone would be a guess."""
    if value is None:
        return None
    text = value.strip()
    try:
        if "T" not in text and " " not in text:
            d = date.fromisoformat(text)
            return f"{d.isoformat()}T00:00:00Z"
        moment = datetime.fromisoformat(text[:-1] + "+00:00"
                                        if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise _bad(f"`{name}`: {value!r} is not an ISO 8601 date or date-time "
                   "(e.g. 2026-10-01 or 2026-10-01T08:00:00+02:00).") from None
    if moment.tzinfo is None:
        raise _bad(f"`{name}`: {value!r} has no time zone. Add `Z` (UTC) or an "
                   "offset (e.g. +02:00); it is converted to UTC.")
    moment = moment.astimezone(timezone.utc)
    spec = "milliseconds" if moment.microsecond else "seconds"
    return moment.replace(tzinfo=None).isoformat(timespec=spec) + "Z"


def need(op: str, **requis: Any) -> None:
    for name, value in requis.items():
        if value is None or value == [] or value == "":
            raise _bad(f"op={op!r} requires `{name}`.")


def _refusal(e: Any, not_found: str | None) -> str:
    """A Modjo refusal (`{code, message}`) in an actionable sentence."""
    body = e.body if isinstance(e.body, dict) else {}
    code = body.get("code") or f"http_{e.status_code}"
    detail = body.get("message") or (e.body if isinstance(e.body, str) else "")
    if e.status_code == 401:
        return (f"Modjo rejected the API key (401, {code}). Create a key in "
                f"{WHERE_TO_CREATE}, then replace the one on the Modjo card.")
    if e.status_code == 403:
        return (f"Modjo: this feature is not enabled for the workspace (403, {code}) "
                f"— it is switched on by Modjo, on the subscription side. {detail}").strip()
    if e.status_code == 404:
        return f"Modjo: not found (404) — {not_found or detail or 'no such object'}."
    if e.status_code == 409:
        return (f"Modjo: not ready or conflicting (409, {code}): {detail} — a call "
                "is analysed 5 to 30 minutes after its import; read it again later.")
    if e.status_code == 410:
        return ("Modjo: deleted for good by the workspace's retention policy (410) — "
                "nothing can be read back.")
    return f"Modjo refused the request (HTTP {e.status_code}, {code}): {detail}"


def run(fn: Callable[[], Any], not_found: str | None = None) -> Any:
    """Local check (ValueError) and 4xx → named refusal; 429 and 5xx stay what
    they are (retryable): the client already waited out one rate window
    (100 requests per minute for the WHOLE workspace, all keys together)."""
    from oto.tools.common import UpstreamHTTPError
    try:
        return fn()
    except ValueError as e:
        raise _bad(f"Modjo: {e}") from None
    except UpstreamHTTPError as e:
        if 400 <= e.status_code < 500 and e.status_code != 429:
            raise _bad(_refusal(e, not_found)) from None
        raise


def paged(res: Any) -> Any:
    """A Modjo list with `next_page` added (null on the last page)."""
    if not isinstance(res, dict) or not isinstance(res.get("pagination"), dict):
        return res
    from oto.tools.modjo import has_next_page
    p = dict(res["pagination"])
    p["next_page"] = p["page"] + 1 if has_next_page(res) else None
    return {**res, "pagination": p}


def page_args(page: Any, size: Any) -> Dict[str, Any]:
    return {"page": page, "size": size}

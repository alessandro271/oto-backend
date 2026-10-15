"""HubSpot — `hubspot_object(op="merge")`: fold duplicate records into one survivor.

No tool here: `hubspot_object` dispatches its `merge` op to `merge()`, like
`hubspot_scopes` and `hubspot_pipelines` serve their neighbours.

**What HubSpot does** (`POST /crm/v3/objects/{type}/merge`, one loser per call): the
primary keeps its own values, an EMPTY property of the primary is filled from the
merged record, and the associations move to the primary. It is IRREVERSIBLE — there
is no unmerge — and a merged id later resolves to the survivor: a GET on it answers
the survivor's record, under the survivor's id. HubSpot may also answer the merge
with a NEW id for the survivor, so the next loser is merged into THAT id, and the
answer names it (`survivor_id`, `survivor_id_changed`).

**Why every loser is read before it is merged.** The read is what tells a loser that
is gone (404 → `not_found`) or already folded into another record (its GET answers
another id → `already_merged`) from one that can be merged — without spending an
irreversible write to find out. It is the same read `dry_run` serves, so the dry run
reports exactly what the real run would decide. ≤ 10 losers keep a call at ≤ 22
requests, far under the private app ceiling (190 / 10 s); the 429 retry is the
client's (`HubSpotClient._request`), and the merges share ONE sleep budget so ten
losers cannot each wait the full cap.

**A loser never stops the batch**: each one gets its own verdict, the others go on.
A refusal on the PRIMARY is not a loser's verdict — it raises, and `hubspot_object`
translates it like any other op's.

Never offered to a recipe: `recipes/ecritures.OPS_DESTRUCTRICES` names `merge`.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from mcp.types import INVALID_PARAMS, ErrorData

from ..mcp_errors import McpError

#: The object types HubSpot's merge endpoint serves. Custom objects are refused: their
#: merge semantics are not documented, and an irreversible write is not a guess.
SUPPORTED = ("companies", "contacts", "deals", "tickets")

#: Losers per call. A dedupe run is reviewed group by group; a bigger batch is a
#: bigger irreversible blast radius for one mistyped id.
MAX_LOSERS = 10

#: What a dry run shows of each record — enough for a human to recognise it.
_SHOWN = {
    "companies": ["name", "domain"],
    "contacts": ["firstname", "lastname", "email"],
    "deals": ["dealname", "amount"],
    "tickets": ["subject"],
}

#: The associations counted on each record — what moves to the primary.
_COUNTED = {
    "companies": ["contacts", "deals"],
    "contacts": ["companies", "deals"],
    "deals": ["contacts", "companies"],
    "tickets": ["contacts", "companies"],
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def validate(object_type, object_id, merge_ids) -> tuple[str, str, list[str]]:
    """`(object_type, primary, losers)` normalised, or a named refusal. Pure."""
    if object_type is None:
        raise _bad("op='merge' requires object_type")
    otype = str(object_type).strip().lower()
    if otype not in SUPPORTED:
        raise _bad(f"op='merge' does not support object_type='{object_type}': HubSpot "
                   f"merges {', '.join(SUPPORTED)} only")
    if object_id is None or str(object_id).strip() == "":
        raise _bad("op='merge' requires object_id = the PRIMARY record (the survivor)")
    primary = str(object_id).strip()
    if not isinstance(merge_ids, list) or not merge_ids:
        raise _bad("op='merge' requires merge_ids = non-empty list of the record ids "
                   "to fold into object_id")
    losers = [str(i).strip() for i in merge_ids]
    if any(not i for i in losers):
        raise _bad("op='merge': merge_ids carries an empty id")
    if len(losers) > MAX_LOSERS:
        raise _bad(f"op='merge' takes at most {MAX_LOSERS} merge_ids per call "
                   f"({len(losers)} given): split the batch")
    if primary in losers:
        raise _bad(f"op='merge': the primary {primary} is also in merge_ids — a record "
                   "cannot be merged into itself")
    doubles = sorted({i for i in losers if losers.count(i) > 1})
    if doubles:
        raise _bad(f"op='merge': merge_ids repeats {doubles} — each loser once")
    return otype, primary, losers


def _message(e) -> str:
    body = getattr(e, "body", None)
    said = body.get("message") if isinstance(body, dict) else body
    return str(said or e)[:300]


def _counts(record: dict, otype: str) -> dict:
    """Associated record counts, from the GET's inline associations. `+` marks a count
    HubSpot paginated: the first page only, so a lower bound."""
    out = {}
    assoc = record.get("associations") or {}
    for kind in _COUNTED[otype]:
        block = assoc.get(kind) or {}
        ids = {str(r.get("id")) for r in block.get("results") or []}
        more = bool((block.get("paging") or {}).get("next"))
        out[kind] = f"{len(ids)}+" if more else len(ids)
    return out


def _summary(record: dict, otype: str) -> dict:
    props = record.get("properties") or {}
    return {"properties": {k: props.get(k) for k in _SHOWN[otype]},
            "associated": _counts(record, otype)}


def _read(c, otype: str, rid: str) -> dict:
    return c.get_object(otype, rid, properties=_SHOWN[otype],
                        associations=_COUNTED[otype]) or {}


def _inspect_loser(c, otype: str, rid: str) -> dict:
    """One loser's state before any write: `live`, `already_merged`, `not_found` or
    `error`. Never raises an upstream refusal — it becomes this loser's verdict."""
    from oto.tools.common.errors import UpstreamHTTPError
    try:
        record = _read(c, otype, rid)
    except UpstreamHTTPError as e:
        if e.status_code == 404:
            return {"id": rid, "status": "not_found"}
        return {"id": rid, "status": "error", "http_status": e.status_code,
                "message": _message(e)}
    resolved = str(record.get("id") or rid)
    if resolved != rid:
        return {"id": rid, "status": "already_merged", "resolves_to": resolved}
    return {"id": rid, "status": "live", **_summary(record, otype)}


def _merge_one(c, otype: str, survivor: str, loser: dict, budget) -> Optional[str]:
    """POST one merge; fills `loser` with its verdict. Returns the survivor's id as
    HubSpot answered it (None when the merge did not happen)."""
    from oto.tools.common.errors import UpstreamHTTPError
    try:
        resp = c._request(
            "POST", f"/crm/v3/objects/{otype}/merge",
            json={"primaryObjectId": survivor, "objectIdToMerge": loser["id"]},
            _budget=budget) or {}
    except UpstreamHTTPError as e:
        if e.status_code == 404:
            loser.update(status="not_found")
        else:
            loser.update(status="error", http_status=e.status_code, message=_message(e))
        return None
    after = str(resp.get("id") or survivor)
    loser.update(status="merged", into=survivor, survivor_id=after)
    return after


def merge(c, object_type, object_id, merge_ids, *, dry_run: bool = False) -> dict:
    """`hubspot_object(op="merge")`. See the module docstring."""
    from oto.tools.hubspot.client import _RetryBudget

    otype, primary, losers = validate(object_type, object_id, merge_ids)

    # The primary: a refusal here is the CALL's (raised, translated by the tool).
    head = _read(c, otype, primary)
    resolved = str(head.get("id") or primary)
    if resolved != primary:
        raise _bad(f"op='merge': the primary {primary} was itself merged into "
                   f"{resolved} — pass object_id='{resolved}' (the record that "
                   "survives today)")

    survivor = primary
    budget = _RetryBudget()
    results = []
    for rid in losers:
        verdict = _inspect_loser(c, otype, rid)
        if verdict["status"] == "already_merged":
            verdict["into_primary"] = verdict["resolves_to"] in (primary, survivor)
        elif verdict["status"] == "live":
            if dry_run:
                verdict["status"] = "would_merge"
            else:
                after = _merge_one(c, otype, survivor, verdict, budget)
                if after is not None:
                    survivor = after
        results.append(verdict)

    out = {
        "op": "merge", "object_type": otype, "dry_run": dry_run,
        "primary_id": primary,
        "survivor_id": survivor,
        "survivor_id_changed": survivor != primary,
        "primary": {"id": primary, **_summary(head, otype)},
        "results": results,
        "counts": dict(Counter(r["status"] for r in results)),
    }
    if dry_run:
        out["would"] = "merge"
        out["note"] = ("nothing written. Without dry_run, each `would_merge` id is "
                       "merged into the primary — IRREVERSIBLE: the primary keeps its "
                       "values, its blanks are filled from the loser, associations "
                       "move to it.")
    return out

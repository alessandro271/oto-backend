"""Resend tools — contacts, the list Broadcasts are sent to.

The first module of the `resend` connector (`tools/resend_segments.py` holds
segments and topics; `resend_socle.py` the shared base: key, refusals, trimmed
views). Sending is NOT here and does not change: `email_send`
(transport=resend) keeps its own path with the same org key.

`resend_contact` reads the list (by segment, or only the unsubscribed), one
contact with its segments and topic subscriptions, and writes it: upsert (one,
or up to 50 with a receipt), update, delete, segment membership. Every write
takes `dry_run`.

Client calls are written out in plain form (`c.upsert_contact(…)`): that is
what the version-skew probe (`test_tools_client_methods_exist`) reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from .resend_socle import (_bad, _client, bad_op, key_refused, need, page_out,
                           props, run, slim, slim_contact, upstream_message)

_BULK_MAX = 50
_ITEM_KEYS = ("email", "first_name", "last_name", "unsubscribed", "properties")
# `op="list"` with `unsubscribed=` walks the list itself (Resend has no filter for
# it): at most this many pages of 100 per call, then it hands back a cursor.
_SCAN_PAGES = 10


def _scan(c, segment_id: Optional[str], after: Optional[str], wanted: bool,
          full: bool) -> dict:
    kept: list = []
    scanned, cursor, has_more = 0, after, False
    for _ in range(_SCAN_PAGES):
        page = c.list_contacts(segment_id, limit=100, after=cursor)
        data = (page or {}).get("data") or []
        scanned += len(data)
        kept += [r for r in data if bool(r.get("unsubscribed")) is wanted]
        has_more = bool((page or {}).get("has_more")) and bool(data)
        if not has_more:
            break
        cursor = data[-1].get("id")
    return {"contacts": kept if full else [slim_contact(r) for r in kept],
            "unsubscribed": wanted, "scanned": scanned, "has_more": has_more,
            "next_after": cursor if has_more else None}


def _read(c, contact: str, full: bool) -> dict:
    """The contact, its segments and its topic subscriptions (three reads)."""
    row = c.get_contact(contact)
    ref = row.get("id") or contact
    segments = c.list_contact_segments(ref, limit=100)
    topics = c.list_contact_topics(ref, limit=100)
    if full:
        return {**row, "segments": (segments or {}).get("data") or [],
                "topics": (topics or {}).get("data") or []}
    return {**slim_contact(row),
            "segments": [{"id": s.get("id"), "name": s.get("name")}
                         for s in (segments or {}).get("data") or []],
            "topics": [{"id": t.get("id"), "name": t.get("name"),
                        "subscription": t.get("subscription")}
                       for t in (topics or {}).get("data") or []]}


def _upsert_many(c, items: Any, segment_ids: Optional[List[str]], dry_run: bool) -> dict:
    """Up to `_BULK_MAX` upserts one after the other (the client paces its calls
    under Resend's rate limit) and a receipt, never N full bodies. A refusal of
    the KEY stops the batch: every remaining item would fail the same way."""
    from oto.tools.common.errors import UpstreamHTTPError

    if not isinstance(items, list) or not items:
        raise _bad("`contacts` must be a non-empty list of objects.")
    if len(items) > _BULK_MAX:
        raise _bad(f"too many contacts ({len(items)}): at most {_BULK_MAX} per call, "
                   "split the list.")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("email"):
            raise _bad(f"`contacts[{i}]` must be an object with an `email`.")
        extra = sorted(set(item) - set(_ITEM_KEYS))
        if extra:
            raise _bad(f"`contacts[{i}]`: unknown key(s) {extra}; accepted: "
                       f"{', '.join(_ITEM_KEYS)}.")
    names = (("would_create", "would_update") if dry_run else ("created", "updated"))
    buckets: Dict[str, list] = {n: [] for n in (*names, "unchanged", "unsubscribed",
                                                "kept_unsubscribed")}
    failed = []
    for i, item in enumerate(items):
        try:
            r = c.upsert_contact(
                item["email"], first_name=item.get("first_name"),
                last_name=item.get("last_name"), unsubscribed=item.get("unsubscribed"),
                properties=item.get("properties"), segment_ids=segment_ids,
                dry_run=dry_run)
        except UpstreamHTTPError as e:
            if key_refused(e):
                raise _bad(upstream_message(e))
            failed.append({"index": i, "email": item["email"],
                           "error": upstream_message(e)})
            continue
        except ValueError as e:
            failed.append({"index": i, "email": item["email"], "error": str(e)})
            continue
        if r["action"] == "would_update":
            buckets["would_update"].append({k: r[k] for k in (
                "email", "changes", "segments_to_add") if k in r})
        else:
            buckets[r["action"]].append(r["email"])
        if r.get("unsubscribed"):
            buckets["unsubscribed"].append(r["email"])
        if r.get("kept_unsubscribed"):
            buckets["kept_unsubscribed"].append(r["email"])
    out: dict = {"total": len(items), "succeeded": len(items) - len(failed)}
    if dry_run:
        out["dry_run"] = True
    out.update({k: v for k, v in buckets.items() if v})
    out["failed"] = failed
    return out


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def resend_contact(
        op: Literal["list", "get", "upsert", "update", "delete",
                    "add_to_segment", "remove_from_segment"] = "list",
        contact: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        unsubscribed: Optional[bool] = None,
        properties: Optional[Dict[str, Any]] = None,
        contacts: Optional[List[Dict[str, Any]]] = None,
        segment_id: Optional[str] = None,
        segment_ids: Optional[List[str]] = None,
        limit: int = 50,
        after: Optional[str] = None,
        full: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Resend contacts — the people Broadcasts are sent to: keep a segment up to
        date (e.g. a newsletter list), see who is in it and who unsubscribed. A
        contact is one email address, global to the Resend account; segments group
        contacts (`resend_segment`), topics carry per-subject opt-outs
        (`resend_topic`). This tool never sends an email.

        `op`:
        - **"list"** (default): contacts, paginated (`limit`, `after`); `segment_id`
          keeps one segment's members. With `unsubscribed=true` (or false) it walks
          up to 1,000 contacts itself and returns only those, plus `scanned` and a
          cursor to continue.
        - **"get"**: one contact (`contact` = id or email) with its `segments` and
          its `topics` subscriptions (opt_in / opt_out).
        - **"upsert"**: create the contact or update what differs, and put it in
          `segment_ids`. One contact (`email` + fields) or up to 50 (`contacts`,
          each `{email, first_name?, last_name?, unsubscribed?, properties?}`) with
          a receipt. Safe to run again. ⚠️ An upsert never resubscribes someone who
          unsubscribed (`kept_unsubscribed` in the receipt); an omitted field is
          never sent, so it never overwrites anything.
        - **"update"**: change fields of an existing contact (`contact`). The only
          way to resubscribe someone (`unsubscribed=false`): do it only on their
          explicit request.
        - **"delete"**: remove a contact (`contact`). To stop emailing someone,
          prefer `update` with `unsubscribed=true`: deleting also drops their
          opt-out, and a later upsert would make them mailable again.
        - **"add_to_segment"** / **"remove_from_segment"**: one contact
          (`contact`) in or out of one segment (`segment_id`).

        `unsubscribed=true` is the GLOBAL opt-out: no Broadcast reaches the contact,
        whatever its segments. `properties` are the account's custom contact
        properties (`{"company": "Acme"}`), as defined in Resend. Needs a Resend
        key with Full access (a sending-only key is refused, and the error says how
        to fix it).

        Returns — list: `{contacts: [{id, email, first_name, last_name,
        unsubscribed, created_at}], has_more, next_after}`, pass `next_after` as
        `after`, `full=true` returns Resend's raw page; get: the contact with
        `properties`, `segments`, `topics`; upsert (one): `{email, id, action:
        created|updated|unchanged, unsubscribed, changes?, segments_added?,
        already_in_segments?, kept_unsubscribed?}`; upsert (many): `{total,
        succeeded, created, updated, unchanged, unsubscribed, kept_unsubscribed,
        failed: [{index, email, error}]}`; with `dry_run=true`, the same after reads
        only (`would_create` / `would_update`, `segments_to_add`).

        Args:
            op: list (default) | get | upsert | update | delete | add_to_segment |
                remove_from_segment.
            contact: op="get"/"update"/"delete"/"add_to_segment"/
                "remove_from_segment" — the contact's id or email.
            email: op="upsert" — one contact's email (or use `contacts`).
            first_name: op="upsert"/"update".
            last_name: op="upsert"/"update".
            unsubscribed: op="upsert"/"update" — the global opt-out to set;
                op="list" — keep only the contacts with this value.
            properties: op="upsert"/"update" — custom properties `{key: value}`
                (string, number, boolean; null clears it on update).
            contacts: op="upsert" — up to 50 contacts, each `{email, first_name?,
                last_name?, unsubscribed?, properties?}`.
            segment_id: op="list" — that segment's members only;
                op="add_to_segment"/"remove_from_segment" — the segment.
            segment_ids: op="upsert" — segments to put the contact(s) in.
            limit: op="list" — page size, 1 to 100 (default 50).
            after: op="list" — cursor: the `next_after` of the previous page.
            full: op="list"/"get" — Resend's raw response instead of the trimmed view.
            dry_run: every write — preview after reads only, nothing written.
        """
        if op == "list":
            if unsubscribed is not None:
                return run(lambda: _scan(_client(), segment_id, after, unsubscribed,
                                         full))
            return run(lambda: page_out("contacts", _client().list_contacts(
                segment_id, limit=limit, after=after), full, slim_contact))

        if op == "get":
            need(contact, "contact", op)
            return run(lambda: _read(_client(), contact, full))

        if op == "upsert":
            if contacts is not None and email:
                raise _bad("op='upsert': pass `email` (one contact) or `contacts` "
                           "(up to 50), not both.")
            if contacts is not None:
                return run(lambda: _upsert_many(_client(), contacts, segment_ids,
                                                dry_run))
            need(email, "email", op)
            return run(lambda: _client().upsert_contact(
                email, first_name=first_name, last_name=last_name,
                unsubscribed=unsubscribed, properties=properties,
                segment_ids=segment_ids, dry_run=dry_run))

        if op == "update":
            need(contact, "contact", op)
            fields = {k: v for k, v in (
                ("first_name", first_name), ("last_name", last_name),
                ("unsubscribed", unsubscribed), ("properties", properties))
                if v is not None}
            if not fields:
                raise _bad("op='update' needs at least one of first_name, last_name, "
                           "unsubscribed, properties.")
            c = _client()
            if dry_run:
                row = run(lambda: c.get_contact(contact))
                have = {**slim_contact(row), "properties": props(row.get("properties"))}
                current = {k: have.get(k) for k in fields if k != "properties"}
                if "properties" in fields:
                    current["properties"] = {k: have["properties"].get(k)
                                             for k in fields["properties"]}
                return {"dry_run": True, "id": row.get("id"), "email": row.get("email"),
                        "current": current, "would_set": fields}
            out = run(lambda: c.update_contact(contact, **fields))
            return {"id": (out or {}).get("id"), "updated": sorted(fields)}

        if op == "delete":
            need(contact, "contact", op)
            c = _client()
            if dry_run:
                return {"dry_run": True,
                        "would_delete": slim_contact(run(lambda: c.get_contact(contact)))}
            return slim(run(lambda: c.delete_contact(contact)))

        if op in ("add_to_segment", "remove_from_segment"):
            need(contact, "contact", op)
            need(segment_id, "segment_id", op)
            target = {"contact": contact, "segment_id": segment_id}
            if dry_run:
                key = "would_add" if op == "add_to_segment" else "would_remove"
                return {"dry_run": True, key: target}
            if op == "add_to_segment":
                run(lambda: _client().add_contact_to_segment(contact, segment_id))
                return {**target, "added": True}
            run(lambda: _client().remove_contact_from_segment(contact, segment_id))
            return {**target, "removed": True}

        raise bad_op(op, "list | get | upsert | update | delete | add_to_segment | "
                         "remove_from_segment")

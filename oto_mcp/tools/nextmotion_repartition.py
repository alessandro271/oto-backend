"""Nextmotion — ONE practitioner's accounting split, on many pricings (or packages) at once.

The API only replaces a pricing's whole per-user list
(`PUT …/user_accounting_distributions`): an omitted user loses their override. Adding
one practitioner to 400 pricings therefore costs a read and a write per pricing, and
a blind write wipes the other practitioners. This module does that read-merge-write
for the caller, in one tool call:

- `assign` — the user gets an accounting distribution on each item, either the SAME
  everywhere (`accounting_distribution` + a list of ids, or every pricing of a
  treatment type) or ONE PER ITEM (`[{id, accounting_distribution}]`): a treatment
  type often mixes lines that are not split the same way;
- `unassign` — the user's override is removed from each item.

Only that user's entry changes; every other entry is written back as it was read. The
preview (`dry_run`, the default) reads every item and returns, item by item, what
would change (`add` | `update` | `remove` | `unchanged`), without writing. A write
goes out only for the items that change; a failing item does not stop the others and
is reported by name (`failed`), never hidden.

Upstream cost is unchanged (a read per item, plus a write per changed item), spread
over `WORKERS` parallel calls, each with its own client.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..mcp_errors import McpError
from .nextmotion_garde import _bad, _run

MAX_ITEMS = 500
WORKERS = 4
PAGE = 100
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _uuid(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise _bad(f"`{name}` must be a UUID — got {value!r}.")
    return value


def plan(op: str, data: Any, *, by_type: bool) -> tuple[str, dict]:
    """`(user, {item_id: accounting_distribution or None})` from `data`, validated before
    any call. `by_type` = the items come from a treatment type, not from `data`."""
    if not isinstance(data, dict):
        raise _bad(f"op={op!r}: `data` must be an object.")
    allowed = {"user", "items"} | ({"accounting_distribution"} if op == "assign" else set())
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _bad(f"op={op!r}: unknown field(s) {unknown} — accepted: {sorted(allowed)}.")
    user = _uuid("data.user", data.get("user"))
    uniform = data.get("accounting_distribution")
    if uniform is not None:
        uniform = _uuid("data.accounting_distribution", uniform)
    items = data.get("items")
    if by_type:
        if items is not None:
            raise _bad(f"op={op!r}: `treatment_type_id` OR `data.items`, not both.")
        if op == "assign" and uniform is None:
            raise _bad("op='assign_distribution' over a treatment type needs "
                       "`data.accounting_distribution` (the same split on every pricing).")
        return user, {}
    if not isinstance(items, list) or not items:
        raise _bad(f"op={op!r}: `data.items` must be a non-empty list (or pass "
                   "`treatment_type_id`).")
    if len(items) > MAX_ITEMS:
        raise _bad(f"op={op!r}: {len(items)} items, at most {MAX_ITEMS} per call — split it.")
    targets: dict = {}
    for n, item in enumerate(items):
        if op == "unassign" or uniform is not None:
            if not isinstance(item, str):
                raise _bad(f"`data.items[{n}]` must be an id"
                           + (" (one `accounting_distribution` for all is given)."
                              if uniform is not None else "."))
            ident, dist = _uuid(f"data.items[{n}]", item), uniform
        else:
            if not isinstance(item, dict) or set(item) != {"id", "accounting_distribution"}:
                raise _bad(f"`data.items[{n}]` must be {{id, accounting_distribution}} "
                           "(or give one `data.accounting_distribution` and a list of ids).")
            ident = _uuid(f"data.items[{n}].id", item["id"])
            dist = _uuid(f"data.items[{n}].accounting_distribution",
                         item["accounting_distribution"])
        if ident in targets:
            raise _bad(f"`data.items`: {ident} appears twice.")
        targets[ident] = dist
    return user, targets


def pricings_of_type(lister: Callable[[int], Any]) -> list:
    """Every pricing of a treatment type (raw rows), page by page; more than
    `MAX_ITEMS` refuses."""
    found: list = []
    offset = 0
    while True:
        env = lister(offset)
        env = env if isinstance(env, dict) else {}
        rows = [r for r in env.get("data") or [] if isinstance(r, dict) and r.get("id")]
        found += rows
        if len(found) > MAX_ITEMS:
            raise _bad(f"more than {MAX_ITEMS} pricings in this treatment type — pass "
                       "`data.items` in several calls.")
        if env.get("next") is None:
            return found
        if not rows:
            raise _bad(f"Nextmotion: empty page while `next` announces more (offset {offset}).")
        offset += len(rows)


def _entries(env: Any, item: str) -> list:
    """`[{user, accounting_distribution: id, name}]` as read; an entry without ids raises."""
    out = []
    for row in (env or {}).get("data") or []:
        dist = row.get("accounting_distribution") if isinstance(row, dict) else None
        if not isinstance(row, dict) or not row.get("user") or not isinstance(dist, dict) \
                or not dist.get("id"):
            raise _bad(f"Nextmotion: unreadable distribution on {item} — nothing written.")
        out.append({"user": row["user"], "accounting_distribution": dist["id"],
                    "name": dist.get("name")})
    return out


def _diff(entries: list, user: str, target: Optional[str]) -> tuple[dict, Optional[list]]:
    """The change for `user` on one item, and the full list to write (`None` = nothing)."""
    mine = next((e for e in entries if e["user"] == user), None)
    before = mine["accounting_distribution"] if mine else None
    others = [{"user": e["user"], "accounting_distribution": e["accounting_distribution"]}
              for e in entries if e["user"] != user]
    if target == before:
        change = "unchanged"
    elif target is None:
        change = "remove"
    else:
        change = "update" if mine else "add"
    row = {"before": before, "before_name": mine["name"] if mine else None,
           "after": target, "change": change}
    if change == "unchanged":
        return row, None
    after = others + ([{"user": user, "accounting_distribution": target}]
                      if target is not None else [])
    return row, after


def read_all(ids: list, *, clients: list, read: Callable[[Any, str], Any]) -> dict:
    """`{id: [{user, accounting_distribution, name}]}` for every item, read in parallel."""
    def one(n_ident: tuple) -> tuple:
        n, ident = n_ident
        c = clients[n % len(clients)]
        return ident, _entries(_run(lambda: read(c, ident)), ident)

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        return dict(pool.map(one, enumerate(ids)))


def run(targets: dict, user: str, *, clients: list, read: Callable[[Any, str], Any],
        write: Callable[[Any, str, list], Any], dry_run: bool) -> dict:
    """Read every item, compute its change, write the changed ones unless `dry_run`."""
    ids = list(targets)

    def one(n_ident: tuple) -> dict:
        n, ident = n_ident
        c = clients[n % len(clients)]
        row: dict = {"id": ident}
        try:
            diff, body = _diff(_entries(_run(lambda: read(c, ident)), ident), user,
                               targets[ident])
            row.update(diff)
            if body is not None and not dry_run:
                _run(lambda: write(c, ident, body))
                row["written"] = True
        except McpError as e:
            row["error"] = e.error.message
        return row

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        rows = list(pool.map(one, enumerate(ids)))
    counts = {k: sum(1 for r in rows if r.get("change") == k)
              for k in ("add", "update", "remove", "unchanged")}
    failed = [r for r in rows if "error" in r]
    out = {"user": user, "items": rows, "counts": counts, "failed": len(failed)}
    if dry_run:
        out.update(dry_run=True, note="Nothing is written. Call again with dry_run=False "
                                      "to write the items whose change is not 'unchanged'.")
    else:
        out.update(written=sum(1 for r in rows if r.get("written")),
                   complete=not failed)
    return out

"""Datastore — the rendered TABLE in the partner front's own look (`data_app`). READ-ONLY.

**Per instance, by configuration**, like `procedure_app`: `OTO_DATA_APP_BUNDLE` (the
URL of the front's bundle, e.g. `https://app.example.com/mcp-app/process-graph.js`,
its stylesheet next to it as `.css`) is both the address and the switch. Set, THIS
module registers `data_app` and the Prefab card of `tools/datastore.py` stands down;
unset, nothing here registers and the Prefab card stays — an instance without such a
front serves no page pointing at nobody. No tenant axis (frozen, `docs/tenants.md`).

One tool, one ui:// template: the bundle draws every answer of `data_app` — a table's
rows, a list of tables, or a one-line note (unknown table, no rows). The view never
calls the backend: what it draws is in `structuredContent`, cut to the page asked for.

**Two channels** (cf. `procedure_app`): the rows go to the view in `structuredContent`,
which a host that paints apps keeps out of the model's context; the MODEL reads one
line. A client that does not paint apps (Claude Code, a runner job) gets the rows as
text instead — it asked to see them, and nothing else will show them.

**The agent picks what matters**: `fields` (the columns, in its order) and `limit`
(25 by default) — a table of forty columns is not forty useful ones. Columns the
owner keeps from agents are absent here as everywhere on the agent face (`agent_access`,
`schema_servi_et_masquees`); the view says how many, never which.
"""
from __future__ import annotations

import html
import json
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import INVALID_PARAMS, ErrorData, TextContent

from ..datastore.identite import AdresseJson as Adresse
from ..mcp_errors import McpError

ENV_BUNDLE = "OTO_DATA_APP_BUNDLE"
URI = "ui://oto/data-table.html"
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
# Meta columns are noise for a reader; `_id` is how an agent acts on a row, so the
# text face (no view) keeps it.
_META = ("_id", "_created_at", "_updated_at")

log = logging.getLogger(__name__)


def bundle() -> str | None:
    """The bundle's URL, or `None`: absent, or not a script's URL — a refusal that is
    logged, since the tool itself does not register without a word."""
    url = (os.environ.get(ENV_BUNDLE) or "").strip()
    if not url:
        return None
    if url.endswith(".js") and urlsplit(url).scheme in ("https", "http"):
        return url
    log.error("%s n'est pas l'URL d'un bundle .js — data_app reste la carte Prefab", ENV_BUNDLE)
    return None


def _text(result: dict, line: str) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=line)], structured_content=result)


def _columns(schema: Optional[dict], rows: list[dict], fields: Optional[list[str]],
             show_meta: bool) -> list[dict]:
    """The columns the view draws, in order: the agent's `fields` when given, else the
    served schema's order, else first appearance in the rows. Each carries its
    declared definition (type, options…) so the front draws the cell as its table
    page does."""
    from ..datastore import schema as dsv2
    declared = {f["key"]: f for f in (schema or {}).get("fields") or []
                if isinstance(f, dict) and f.get("key")}
    if fields:
        keys = [k for k in fields if k != "*"]
    else:
        keys = list(declared)
        for r in rows:
            for k in r:
                if k not in keys and k not in _META:
                    keys.append(k)
    if show_meta:
        keys += [k for k in _META if k not in keys]
    title_key = (dsv2.title_field(schema) or {}).get("key")
    return [{**declared.get(k, {"key": k}), "key": k, "is_title": k == title_key} for k in keys]


def register(mcp: FastMCP) -> None:
    url = bundle()
    if url is None:
        return
    try:
        from fastmcp.apps import AppConfig, ResourceCSP, UI_MIME_TYPE
    # noqa: SILENT — extra `apps` absent ⇒ no app, data_rows is enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    from .. import access
    from ..datastore.core import DatastoreNotFound, InvalidCursor
    from ..procedure_diagram import client_affiche_les_apps
    from .datastore import _ns, _store_for
    from .procedure_app import _LOGO_DOMAINS, _SHELL, _origin

    page = _SHELL.format(js=html.escape(url, quote=True),
                         css=html.escape(url[:-3] + ".css", quote=True))
    origin = _origin(url)

    @mcp.resource(URI, name="data_table", mime_type=UI_MIME_TYPE,
                  app=AppConfig(csp=ResourceCSP(resource_domains=[origin, *_LOGO_DOMAINS]),
                                prefers_border=True))
    def data_table_page() -> str:
        return page

    @mcp.tool(app=AppConfig(resource_uri=URI))
    def data_app(
        datastore: Optional[Adresse] = None,
        fields: Optional[list[str]] = None,
        filter: Optional[dict] = None,
        q: Optional[str] = None,
        order_by: Optional[str] = None,
        order_dir: str = "desc",
        limit: int = DEFAULT_LIMIT,
        cursor: Optional[str] = None,
        show_meta: bool = False,
    ) -> ToolResult:
        """SHOW a table to the user: draws its rows INLINE in the conversation, in the
        app's own table look (typed cells, select colours, links). READ-ONLY.

        Use it whenever the user wants to SEE or browse a table's content; then answer
        in text. WITHOUT `datastore` = the list of your tables. To read rows yourself,
        use `data_rows` — this view gives you only a one-line summary.

        CHOOSE the columns: pass `fields` with only those that matter for the question
        (in the order to show them) — most tables have more columns than a reader
        needs. Omitted = the declared columns, in schema order. Keep `limit` small:
        the user scrolls and asks for more; the default (25) is usually right.

        Sort, filter and search are arguments, same grammar as `data_rows`: call again
        to change them. Next page = the `next_cursor` of the previous call, with the
        SAME `order_by`/`q` (a cursor never crosses from one to the other).

        Args:
            datastore: the table's NUMBER (`ns_id`), or `slot:<name>`; omit = list your tables.
            fields: the columns to show, in order; omit = the declared ones.
            filter: `{column: value}` or `{column: {op: value}}`, as in `data_rows`.
            q: free-text search, as in `data_rows`.
            order_by: sort column (user or `_created_at`/`_updated_at`).
            order_dir: `desc` (default) or `asc`.
            limit: rows shown (default 25, max 200).
            cursor: `next_cursor` of the previous call, for the next page.
            show_meta: also show `_id`/`_created_at`/`_updated_at`.
        """
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        affiche = client_affiche_les_apps()

        if datastore is None or datastore == "":
            tables = [{"ns_id": s["ns_id"], "name": s["datastore"],
                       "typed": bool((s.get("schema") or {}).get("fields")),
                       "shared": bool(s.get("shared")), "url": s.get("url")}
                      for s in store.list_datastores()]
            result = {"view": "tables", "tables": tables, "origin": origin}
            # The numbers go to the model too: without them it guesses a name or a
            # number to show one (measured: "event log", then a number that did not exist).
            index = ", ".join(f"{t['ns_id']} {t['name']}" for t in tables) or "none"
            if not affiche:
                return _text(result, f"Your tables (number name): {index}.")
            return _text(result, f"{len(tables)} table(s) shown to the user (number name): "
                                 f"{index}. To show one: data_app datastore=<number>.")

        ref = _ns(str(datastore))
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        projection = None
        if fields:
            projection = list(fields)
            if show_meta:
                projection += [k for k in _META if k not in projection]
        try:
            schema, hidden = store.schema_servi_et_masquees(ref)
            page_ = store.cursor_rows(ref, filter=filter, limit=limit, cursor=cursor, q=q,
                                      order_by=order_by, order_dir=order_dir,
                                      fields=projection)
            numero = dict(store.dernier_tableau or {})
            total = store.count_rows(ref, filter=filter, q=q)
            table_url = store.get_url(ref)
        except DatastoreNotFound:
            message = f"No table “{datastore}” in your active org or your teams."
            return _text({"view": "note", "message": message, "origin": origin},
                         message + " Their numbers: data_app without datastore.")
        except (ValueError, InvalidCursor) as e:
            # A malformed filter or a cursor from another regime: refused by name — an
            # empty table would read as "no such data" (oto#74).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

        rows = page_["rows"]
        if not show_meta:
            rows = [{k: v for k, v in r.items() if k not in _META or k == "_id"} for r in rows]
        name = numero.get("datastore") or str(datastore)
        columns = _columns(schema, rows, fields, show_meta)
        result = {
            "view": "table", "ns_id": numero.get("ns_id"), "name": name,
            "columns": columns, "rows": rows, "count": len(rows), "total": total,
            "next_cursor": page_.get("next_cursor"), "hidden_columns": hidden,
            "filter": filter, "q": q, "order_by": order_by, "order_dir": order_dir,
            "table_url": table_url, "origin": origin,
        }
        if not affiche:
            # No view to draw them: the rows ARE the answer.
            body = json.dumps(rows, ensure_ascii=False, default=str)
            return _text(result, f"{name} — {len(rows)} of {total} row(s):\n{body}")
        line = (f"Table {numero.get('ns_id')} {name} — {len(rows)} of {total} row(s) shown to the user, columns: "
                f"{', '.join(c['key'] for c in columns) or '—'}.")
        if page_.get("next_cursor"):
            line += f" More: same call with cursor={page_['next_cursor']}."
        return _text(result, line + " Do not call this again for the same question."
                                    " To read rows yourself: data_rows.")

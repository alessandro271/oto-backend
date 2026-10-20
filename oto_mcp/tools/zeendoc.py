"""Zeendoc tool — document management (GED), read-only.

Wraps `oto.tools.zeendoc.ZeendocClient` (REST API v4 and the document pages, one
session cookie), credential resolved per call via
`access.resolve_credential_fields("zeendoc")`: a Zeendoc user's login and password
and the cabinet id (`url_client`). The user's rights bound every read.

The connector is read-only, so it serves ONE tool, `zeendoc` (LECTURE), the read
verb in `op` — no `zeendoc_act` (`tests/test_convention_act.py`).

A document's text and file go out through `file_content.render_for_agent`, the
single home of the inline-vs-signed-URL rule: the text inline, a PDF as its
extracted text plus `raw_url` to the original, never base64 in the agent's context.

Client calls are written out (`_client().search_documents(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import LECTURE

_NAME = "zeendoc"

_OPS = ("binders", "choices", "values", "search", "count", "document", "text", "file")

#: What each op reads; any other argument given is refused, never dropped.
_TAKES = {
    "binders": {"full"},
    "choices": {"binder", "field"},
    "values": {"binder", "field", "term"},
    "search": {"binder", "criteria", "match", "order_by", "order", "offset", "limit",
               "columns", "full"},
    "count": {"binder", "criteria", "match"},
    "document": {"binder", "res_id", "columns", "full"},
    "text": {"binder", "res_id"},
    "file": {"binder", "res_id", "version"},
}
_NEEDS = {
    "choices": ("binder", "field"),
    "values": ("binder", "field", "term"),
    "search": ("binder",),
    "count": ("binder",),
    "document": ("binder", "res_id"),
    "text": ("binder", "res_id"),
    "file": ("binder", "res_id"),
}

#: A list field shows its choices in op='binders' up to this many; beyond, their
#: count, and op='choices' lists them.
_CHOICES_INLINE = 30

#: Properties a search repeats in another form, dropped from the default view.
_PROPERTY_DROP = ("displayableCreationDate", "creationDateUtc", "creationDateUTC")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _check_args(op: str, given: dict) -> None:
    """Required arguments named, ignored ones refused: nothing is done either way."""
    for name in _NEEDS.get(op, ()):
        if given.get(name) in (None, ""):
            raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")
    stray = sorted(k for k, v in given.items() if v is not None and k not in _TAKES[op])
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")


def _upstream_message(e) -> str:
    """Turn a Zeendoc refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body if isinstance(e.body, (str, dict)) else ""
    if status == 400:
        return f"Zeendoc rejected the request as invalid (400): {detail}"
    if status == 401:
        return ("Zeendoc refused the login (401): the login, the password or the cabinet "
                "id set on the connector is wrong, or the user is disabled.")
    if status == 403:
        return (f"Zeendoc refused the call (403): this user has no right on that binder "
                f"or that service. {detail}")
    if status == 404:
        return (f"Zeendoc: not found (404) — check the binder, the field or the "
                f"document id. {detail}")
    if status == 413:
        return f"Zeendoc: file too large to serve (413). {detail}"
    if status == 429:
        return "Zeendoc: too many requests (429) — retry later."
    if status in (500, 502, 503, 504):
        return f"Zeendoc could not serve the request (HTTP {status}). {detail}"
    return f"Zeendoc refused the request (HTTP {status}): {detail}"


def _choices(entries: Any) -> list:
    """A list field's choices, flat: a dependent list (or an accounting split) groups
    them under their parent's label, kept as `group`."""
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if "membreGroupe" in entry or "nomGroupe" in entry:
            for item in entry.get("membreGroupe") or []:
                row = {"id": item.get("id"), "label": item.get("label")}
                if entry.get("nomGroupe"):
                    row["group"] = entry["nomGroupe"]
                out.append(row)
        else:
            out.append({"id": entry.get("id"), "label": entry.get("label")})
    return out


def _binders(context: Any) -> dict:
    """The default view of the context: each binder with its rights and its fields,
    list choices inline while short; the cross-binder search entry left out."""
    binders = []
    for coll in context.get("collections") or []:
        if not coll.get("collId"):
            continue
        fields = []
        for f in coll.get("index") or []:
            row = {"field": f.get("indexId"), "label": f.get("label"), "type": f.get("type")}
            if f.get("isMultivalued"):
                row["multivalued"] = True
            choices = _choices(f.get("possibleValue") or f.get("possibleValueCreditdebit"))
            if len(choices) > _CHOICES_INLINE:
                row["choices_count"] = len(choices)
            elif choices:
                row["choices"] = choices
            fields.append(row)
        binders.append({"binder": coll["collId"], "label": coll.get("label"),
                        "rights": coll.get("rights"), "fields": fields})
    return {"binders": binders, "statuses": (context.get("client") or {}).get("status")}


def _document(doc: dict) -> dict:
    """A document without empty index values nor repeated properties."""
    return {
        **doc,
        "properties": {k: v for k, v in (doc.get("properties") or {}).items()
                       if k not in _PROPERTY_DROP},
        "indexes": {k: v for k, v in (doc.get("indexes") or {}).items() if v not in ([], None)},
        "displayableIndexes": {k: v for k, v in (doc.get("displayableIndexes") or {}).items()
                               if v not in ([], None)},
    }


def _found(result: Any) -> dict:
    """The default view of a search: the total, the sums of the amount fields, and
    each document trimmed."""
    stats = {k: {m: s.get(m) for m in ("sum", "count", "min", "max")}
             for k, s in (result.get("stats") or {}).items() if isinstance(s, dict)}
    return {"nbDocs": result.get("nbDocs"), "stats": stats,
            "document": [_document(d) for d in result.get("document") or []]}


def _render(data: bytes, filename: str, mime: str) -> dict:
    sub = access.current_user_sub_or_raise()
    try:
        return file_content.render_for_agent(data, filename, mime, sub=sub,
                                             prefix="zeendoc-files")
    except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
        raise _bad(f"Zeendoc: {e}") from None


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: logs in and reads the user's binders (`probe`) — no
    side effect; 401 on a wrong login, password or cabinet id."""
    from oto.tools.zeendoc import ZeendocClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        ZeendocClient(login=fields.get("login"), password=fields.get("password"),
                      url_client=fields.get("url_client")).probe()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Zeendoc HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Zeendoc HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.zeendoc import ZeendocClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> ZeendocClient:
        creds = access.resolve_credential_fields(_NAME)
        return ZeendocClient(login=creds.get("login"), password=creds.get("password"),
                             url_client=creds.get("url_client"))

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    @mcp.tool(name="zeendoc", annotations=LECTURE)
    def zeendoc(
        op: Literal[_OPS] = "binders",
        binder: Optional[str] = None,
        criteria: Optional[list[dict]] = None,
        match: Optional[Literal["all", "any"]] = None,
        order_by: Optional[str] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        columns: Optional[list[str]] = None,
        res_id: Optional[int] = None,
        field: Optional[str] = None,
        term: Optional[str] = None,
        version: Optional[Literal["pdf", "original"]] = None,
        full: bool = False,
    ) -> Any:
        """Zeendoc document management (GED), read-only: the binders of the cabinet
        (supplier invoices, customer invoices, expense reports…), search and count of
        their documents by indexing field or full text, and each document's values,
        text and PDF. Reads see what the connector's Zeendoc user sees.

        `op`:
        - `binders` (start here) — each binder (`coll_1`…) with the user's rights and
          its fields: `field` name (`custom_t2`), label, type, list choices.
        - `choices` — every choice of a list `field` (id, label, documents using it).
        - `values` — values of a text `field` that start like `term`, to get the exact
          spelling (a supplier's name) before an EQUALS.
        - `search` — documents of `binder` matching `criteria`, newest first or by
          `order_by`; pages of at most 50 (`offset`, `limit`); `nbDocs` is the total,
          `stats` the sum, count, min and max of the amount fields over ALL matches.
        - `count` — `{nbDocs}` for the same criteria.
        - `document` — one document by `res_id`, as a search returns it.
        - `text` — the text Zeendoc extracted (OCR), laid out as on the page.
        - `file` — the PDF (`version="pdf"`, with Zeendoc's stamps) or the file as
          received (`version="original"`): a PDF comes as its extracted text plus
          `raw_url`, a short-lived link to the file.

        `criteria`: a list of `{field, operator, value}`, or `{field, operator:
        "BETWEEN", start, end}`, joined by `match` (all = AND, any = OR). `field`: a
        binder field, `Plain_Text` (full text), `Res_Id`, `Creation_Date`, `N_Status`
        or `Filename`. Operators: EQUALS (whole value, any case), DIFFERS, CONTAINS
        (whole words, `*` wildcard), DO_NOT_CONTAINS, NULL, ABOVE, LOWER, BETWEEN.
        Dates YYYY-MM-DD; `|` in a value means OR; a list field is filtered by the
        id of its choice. Each document gives `indexes` (stored: ids, ISO dates,
        numbers) and `displayableIndexes` (labels, French formats), both lists per
        field. Deleted documents only on `N_Status` EQUALS -1. `full=true` returns
        the raw response.

        Args:
            op: binders | choices | values | search | count | document | text | file.
            binder: the binder id, e.g. coll_2, from op='binders'.
            criteria: search / count — list of {field, operator, value | start, end}.
            match: search / count — all (AND, default) | any (OR).
            order_by: search — a field or property to sort on.
            order: search — asc | desc (default) with order_by.
            offset: search — documents to skip, from 0.
            limit: search — documents per page, 1 to 50 (default 50).
            columns: search / document — only these fields and properties.
            res_id: document / text / file — the document id (resId).
            field: choices / values — the field name, e.g. custom_t2.
            term: values — the beginning of the value.
            version: file — pdf (default) | original.
            full: binders / search / document — raw response instead of the view.
        """
        given = {"binder": binder, "criteria": criteria, "match": match,
                 "order_by": order_by, "order": order, "offset": offset, "limit": limit,
                 "columns": columns, "res_id": res_id, "field": field, "term": term,
                 "version": version, "full": full or None}
        _check_args(op, given)
        if op == "binders":
            context = _run(lambda: _client().get_context())
            return context if full else _binders(context)
        if op == "choices":
            return _run(lambda: _client().list_field_choices(binder, field))
        if op == "values":
            return _run(lambda: _client().suggest_field_values(binder, field, term))
        if op == "count":
            return _run(lambda: _client().count_documents(
                binder, criteria, match=match or "all"))
        if op == "search":
            result = _run(lambda: _client().search_documents(
                binder, criteria, match=match or "all", order_by=order_by,
                order=order or "desc", offset=offset or 0, limit=limit or 50,
                columns=columns))
            return result if full else _found(result)
        if op == "document":
            doc = _run(lambda: _client().get_document(binder, res_id, columns=columns))
            return doc if full else _document(doc)
        if op == "text":
            text = _run(lambda: _client().get_document_text(binder, res_id))
            return _render(text.encode("utf-8"), f"{binder}_{res_id}.txt",
                           "text/plain; charset=utf-8")
        f = _run(lambda: _client().download_document(binder, res_id, version=version or "pdf"))
        return _render(f["content"], f["filename"], f["content_type"])

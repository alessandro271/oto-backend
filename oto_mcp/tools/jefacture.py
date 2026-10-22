"""jefacture.com tool — French e-invoicing (Banqup platform), read-only.

Credential = the PERSON's jefacture.com sign-in (OAuth 2.0 authorization code),
acquired and renewed by `auth/jefacture.py`. Wraps the lib's
`oto.tools.jefacture.JefactureClient`, one op per client method, same names.

The connector is read-only, so it serves ONE tool, `jefacture` (LECTURE), the read
verb in `op` — no `jefacture_act` (`tests/test_convention_act.py`).

Every argument is checked against what `op` reads (`_ARGS`): one it does not read is
REFUSED, never silently dropped, and a missing required one is named. The client
validates values (UUIDs, dates, closed choices) and its `ValueError` comes back as
the argument refusal it is. A 401 drops the cached access token and retries ONCE
with a renewed one; another 4xx becomes a named refusal, the body fenced; 429 and
5xx stay what they are, the error taxonomy classifies them as retryable.

Client calls are written out (`_client().list_documents(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError
from ..texte_tiers import cloture
from .lecture import LECTURE

_NAME = "jefacture"

_PAGE = ("page", "page_size")

#: op → the arguments it reads (the client method's, same names), REQUIRED first.
_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "get_me": ((), ()),
    "list_spaces": ((), _PAGE),
    "list_documents": (("space_id",), ("folder", "category", "status_query",
                                       "issued_from", "issued_to", "due_from", "due_to",
                                       "updated_since", "document_number", "counterpart",
                                       "sort_by", *_PAGE)),
    "get_document": (("space_id", "document_id"), ()),
    "list_balance_offsets": (("space_id", "document_id"), ()),
    "get_payment": (("space_id", "payment_id"), ()),
    "list_reconciliations": (("space_id",), ("matching_status", *_PAGE)),
    "list_connections": (("space_id",), ("relation_type", "search", "identifier", *_PAGE)),
    "get_connection": (("space_id", "connection_id"), ()),
    "list_payment_accounts": (("space_id",), ()),
    "list_account_movements": (("space_id", "payment_account_id"),
                               ("booking_type", "date_from", "date_to", *_PAGE)),
}
_OPS = tuple(_ARGS)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _check(op: str, given: dict) -> None:
    """No argument is silently dropped: one that `op` does not read is refused, a
    required one that is missing is named."""
    required, optional = _ARGS[op]
    stray = sorted(k for k, v in given.items() if v is not None
                   and k not in required and k not in optional)
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")
    missing = [k for k in required if given.get(k) in (None, "")]
    if missing:
        raise _bad(f"op='{op}' requires {', '.join(f'`{k}`' for k in missing)}"
                   + (" (from op='list_spaces')" if "space_id" in missing else "")
                   + ". Nothing was done.")


def _upstream_message(e) -> str:
    """A jefacture.com refusal (4xx but 401 and 429), by status, its body fenced."""
    status = e.status_code
    if status == 400:
        texte = ("jefacture.com rejected the request as invalid (400): check the "
                 "arguments (ids as UUIDs, dates as YYYY-MM-DD, page from 0).")
    elif status == 403:
        texte = ("jefacture.com refused the read (403): the person has no right on this "
                 "space or this resource, or the application lacks the API product it "
                 "needs.")
    elif status == 404:
        texte = ("jefacture.com: not found (404) — check the space id and the object "
                 "id.")
    else:
        texte = f"jefacture.com refused the request (HTTP {status})."
    if e.body in (None, "", {}):
        return texte
    return texte + cloture("upstream-error-body", str(e.body),
                           origine="Refusal written by jefacture.com",
                           lecture="a diagnostic")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.jefacture import JefactureClient

    from ..auth import jefacture as jf_auth

    def _client() -> JefactureClient:
        return JefactureClient(access_token=jf_auth.access_token_for(
            access.current_user_sub_or_raise()))

    def _run(fn):
        for tentative in (1, 2):
            try:
                return fn()
            except UpstreamHTTPError as e:
                if e.status_code == 401:
                    jf_auth.oublier_jeton(access.current_user_sub_or_raise())
                    if tentative == 1:
                        continue
                    raise _bad("jefacture.com rejects the authorization (HTTP 401) even "
                               "with a renewed token: reconnect from your connectors, "
                               "« jefacture.com ».")
                if 400 <= e.status_code < 500 and e.status_code != 429:
                    raise _bad(_upstream_message(e))
                raise
            except RuntimeError as e:
                # Dead grant, application refused or not configured, core missing.
                raise _bad(str(e))
            except ValueError as e:
                # The client's argument check (UUID, date, closed choice, bounds).
                raise _bad(f"{e} Nothing was done.")

    @mcp.tool(name="jefacture", annotations=LECTURE)
    def jefacture(
        op: Literal[_OPS] = "list_spaces",
        space_id: Optional[str] = None,
        document_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        payment_account_id: Optional[str] = None,
        folder: Optional[str] = None,
        category: Optional[str] = None,
        status_query: Optional[str] = None,
        issued_from: Optional[str] = None,
        issued_to: Optional[str] = None,
        due_from: Optional[str] = None,
        due_to: Optional[str] = None,
        updated_since: Optional[str] = None,
        document_number: Optional[str] = None,
        counterpart: Optional[str] = None,
        sort_by: Optional[str] = None,
        matching_status: Optional[str] = None,
        relation_type: Optional[str] = None,
        search: Optional[str] = None,
        identifier: Optional[str] = None,
        booking_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Any:
        """jefacture.com e-invoicing, read-only, with the person's own sign-in: invoices
        and credit notes issued and received, their statuses, what remains due, the
        payments matched to them, customers and suppliers, payment accounts.

        Start with `op="list_spaces"`: a space is one company, its `id` is the
        `space_id` every other op needs (`get_me` excepted).

        `op`:
        - `get_me` — who the sign-in acts as. `list_spaces` — the spaces reached.
        - `list_documents` — invoices and credit notes of a space. `folder`: SALES =
          issued, PURCHASES = received. `status_query` in the platform's query
          language, e.g. `pay=UNPAID OR pay=PARTIALLY_PAID`. Dates YYYY-MM-DD, inclusive.
        - `get_document` (with its lines), `list_balance_offsets` (payments imputed
          to it) — `document_id`. `get_payment` — `payment_id`, from a balance offset.
        - `list_reconciliations` — documents with their matched or proposed payments.
        - `list_connections`, `get_connection` — customers and suppliers.
        - `list_payment_accounts` — accounts and balances; `list_account_movements`
          — movements of one (`payment_account_id`).

        What remains due is `openBalance` (`metadata.documentTotals`). Lists are paged
        from 0 (`page=0` is the first). An argument the op does not read is refused.
        Not here: bookkeeping entries, the balance sheet, sales to private customers
        without an invoice.

        Args:
            op: the read, see above.
            space_id: every op but get_me / list_spaces — from op='list_spaces'.
            document_id: get_document / list_balance_offsets — from list_documents.
            payment_id: get_payment — `paymentId` of a balance offset.
            connection_id: get_connection — from list_connections.
            payment_account_id: list_account_movements — from list_payment_accounts.
            folder: list_documents — SALES | PURCHASES | DRAFTS | INBOX.
            category: list_documents — INVOICE | CREDIT_NOTE | SELF_BILLED_INVOICE |
                SELF_BILLED_CREDIT_NOTE | PROFORMA_INVOICE | INVOICE_REMINDER |
                RECEIPT | QUOTE.
            status_query: list_documents — filter on status.document / pay / delivery,
                e.g. `pay=UNPAID OR pay=PARTIALLY_PAID`.
            issued_from: list_documents — issued on or after, YYYY-MM-DD.
            issued_to: list_documents — issued on or before, YYYY-MM-DD.
            due_from: list_documents — due on or after, YYYY-MM-DD.
            due_to: list_documents — due on or before, YYYY-MM-DD.
            updated_since: list_documents — updated since, date or ISO 8601 date-time.
            document_number: list_documents — part of the document number.
            counterpart: list_documents — part of the customer's or supplier's name.
            sort_by: list_documents — issueDate.desc | issueDate.asc | dueDate.asc |
                dueDate.desc | updatedAt.desc | updatedAt.asc | openBalance.desc |
                payableAmount.desc | documentNumber.asc.
            matching_status: list_reconciliations — MATCHED | PROPOSED | UNMATCHED.
            relation_type: list_connections — CUSTOMER | SUPPLIER.
            search: list_connections — part of the name.
            identifier: list_connections — a legal identifier, e.g. `FR:SIREN:123456789`.
            booking_type: list_account_movements — BOOKED | PLANNED | RESERVED.
            date_from: list_account_movements — from this date, YYYY-MM-DD.
            date_to: list_account_movements — up to this date, YYYY-MM-DD.
            page: lists — page number, from 0.
            page_size: lists — items per page (at most 200; 100 for movements).
        """
        _check(op, {
            "space_id": space_id, "document_id": document_id, "payment_id": payment_id,
            "connection_id": connection_id, "payment_account_id": payment_account_id,
            "folder": folder, "category": category, "status_query": status_query,
            "issued_from": issued_from, "issued_to": issued_to, "due_from": due_from,
            "due_to": due_to, "updated_since": updated_since,
            "document_number": document_number, "counterpart": counterpart,
            "sort_by": sort_by, "matching_status": matching_status,
            "relation_type": relation_type, "search": search, "identifier": identifier,
            "booking_type": booking_type, "date_from": date_from, "date_to": date_to,
            "page": page, "page_size": page_size,
        })
        if op == "get_me":
            return _run(lambda: _client().get_me())
        if op == "list_spaces":
            return _run(lambda: _client().list_spaces(page, page_size))
        if op == "list_documents":
            return _run(lambda: _client().list_documents(
                space_id, folder=folder, category=category, status_query=status_query,
                issued_from=issued_from, issued_to=issued_to, due_from=due_from,
                due_to=due_to, updated_since=updated_since,
                document_number=document_number, counterpart=counterpart,
                sort_by=sort_by, page=page, page_size=page_size))
        if op == "get_document":
            return _run(lambda: _client().get_document(space_id, document_id))
        if op == "list_balance_offsets":
            return _run(lambda: _client().list_balance_offsets(space_id, document_id))
        if op == "get_payment":
            return _run(lambda: _client().get_payment(space_id, payment_id))
        if op == "list_reconciliations":
            return _run(lambda: _client().list_reconciliations(
                space_id, matching_status=matching_status, page=page,
                page_size=page_size))
        if op == "list_connections":
            return _run(lambda: _client().list_connections(
                space_id, relation_type=relation_type, search=search,
                identifier=identifier, page=page, page_size=page_size))
        if op == "get_connection":
            return _run(lambda: _client().get_connection(space_id, connection_id))
        if op == "list_payment_accounts":
            return _run(lambda: _client().list_payment_accounts(space_id))
        return _run(lambda: _client().list_account_movements(
            space_id, payment_account_id, booking_type=booking_type,
            date_from=date_from, date_to=date_to, page=page, page_size=page_size))

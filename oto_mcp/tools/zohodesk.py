"""Zoho Desk — support tickets, threads, contacts, articles (Help Center KB).

Credential = OAuth2 (self-client) with 5 fields: client_id + client_secret +
refresh_token + org_id (`orgId` header required) + data_center (region, non-secret)
→ generic multi-field model (ADR 0011), resolved per call via
`access.resolve_credential_fields("zohodesk")`. byo_user. Access token derived/cached
in memory on the client side.
"""
from __future__ import annotations

from typing import Optional

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, status_hints
from ..connectors import verify as connector_verify

# Zoho hosts per regional data center: the Desk API AND the OAuth refresh are tied to
# their issuing region (a `.eu` self-client hitting `desk.zoho.com`/`accounts.zoho.com`
# is rejected with an opaque `invalid_client` — same gotcha as the CRM connector). The
# credential's `data_center` field selects the API domains (`desk.zoho.<tld>`) and
# OAuth domains (`accounts.zoho.<tld>`). Recognized regions:
_DC_DOMAINS = {
    "com": ("https://desk.zoho.com", "https://accounts.zoho.com"),
    "eu": ("https://desk.zoho.eu", "https://accounts.zoho.eu"),
    "in": ("https://desk.zoho.in", "https://accounts.zoho.in"),
    "au": ("https://desk.zoho.com.au", "https://accounts.zoho.com.au"),
    "jp": ("https://desk.zoho.jp", "https://accounts.zoho.jp"),
    "ca": ("https://desk.zohocloud.ca", "https://accounts.zohocloud.ca"),
}


def _resolve_dc_domains(data_center: Optional[str]) -> tuple[str, str]:
    """`(api_domain, accounts_url)` for the declared Zoho Desk region. Missing
    or unrecognized region → actionable `McpError`, **never** a silent fallback to `com`
    (that fallback masked the real cause of an `invalid_client`: self-client set up on
    another region). `com` remains fully valid — we just require a recognized choice."""
    dc = (data_center or "").strip().lower()
    if dc not in _DC_DOMAINS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            (f"Unrecognized Zoho data center: {data_center!r}." if dc
             else "Zoho data center missing.")
            + " Fill in your region in the \"Data center\" field of the Zoho Desk connector —"
            " one of: com, eu, in, au, jp, ca. It is visible in the URL when you are"
            " signed in to Zoho Desk (e.g. desk.zoho.eu → \"eu\", desk.zoho.com → \"com\")."
        )))
    return _DC_DOMAINS[dc]


# Desk surface → OAuth scope that unlocks it. Serves the probe AND the diagnosis: a
# raw `SCOPE_MISMATCH` from Zoho does NOT say which scope is missing (feedback #299),
# while that is the only information needed to regenerate the self-client.
_DESK_SCOPES = (
    ("departments", "Desk.basic.READ", lambda c: c.list_departments()),
    ("tickets", "Desk.tickets.READ", lambda c: c.list_tickets(limit=1)),
    ("contacts", "Desk.contacts.READ", lambda c: c.list_contacts(limit=1)),
    ("articles (KB)", "Desk.articles.READ", lambda c: c.list_articles(limit=1)),
)


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001 (probe contract)
    """Probe with NO side effects, in two steps — same pattern as the CRM connector.

    1. **OAuth refresh**: validates client_id + client_secret + refresh_token + region.
    2. **real read of each Desk surface** (`per_page`/`limit` = 1): a Zoho
       token can authenticate with PARTIAL scopes — a real case, articles
       answered 200 while tickets/contacts/departments returned an opaque
       `SCOPE_MISMATCH`. No readable surface ⇒ failure, citing the granted scope
       and the missing ones (the credential is unusable). At least one
       readable ⇒ success: the credential works, even if restricted.
    """
    from oto.tools.zohodesk.client import ZohoDeskClient

    from .zoho import _zoho_error_hint  # same family, single source of the diagnosis

    status_hints.require_complete("zohodesk", fields)
    api_domain, accounts_url = _resolve_dc_domains(fields.get("data_center"))
    try:
        tok = requests.post(f"{accounts_url}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": fields.get("client_id"),
            "client_secret": fields.get("client_secret"),
            "refresh_token": fields.get("refresh_token"),
        }, timeout=20).json()
    except Exception as e:  # noqa: BLE001 — network / unreadable response
        raise ValueError(f"Zoho Desk connection failed: {type(e).__name__}") from e
    if "access_token" not in tok:
        raise ValueError(_zoho_error_hint(tok.get("error") or tok))
    granted = tok.get("scope", "")

    client = ZohoDeskClient(
        client_id=fields.get("client_id"), client_secret=fields.get("client_secret"),
        refresh_token=fields.get("refresh_token"), org_id=fields.get("org_id"),
        api_domain=api_domain, accounts_url=accounts_url,
    )
    missing: list[str] = []
    for label, scope, call in _DESK_SCOPES:
        try:
            call(client)
            return  # at least one readable surface → credential usable
        # noqa: SILENT — missing scope collected then returned in the probe message
        except Exception as e:  # noqa: BLE001 — the provider error IS the probe's return
            if "SCOPE" in str(e).upper():
                missing.append(f"{label} → {scope}")
    if missing:
        extra = f" (granted scope: {granted})" if granted else ""
        raise ValueError(
            "the token authenticates but opens NO Zoho Desk surface" + extra
            + " — expected scopes: " + " ; ".join(missing)
            + ". Regenerate the Desk self-client with these scopes.")
    raise ValueError("Zoho Desk connection established but no readable surface "
                     "(wrong org_id, or departments/tickets inaccessible).")


# Help Center articles — the DEFAULT of a listing or a search is a light view: Zoho
# answers each article with its owner, author, translations, counters… several
# hundred characters an agent never reads, times 50 per page. What stays is what is
# needed to pick an article; Zoho's own `summary` is its excerpt of the body. The
# full HTML body is never in a listing, at any level: it comes from
# `zohodesk_article`, and each description says so.
_ARTICLE_KEYS = ("id", "title", "status", "summary", "modifiedTime", "webUrl",
                 "portalUrl")


def _slim_article(article: dict) -> dict:
    out = {k: article[k] for k in _ARTICLE_KEYS if k in article}
    category = article.get("category")
    if isinstance(category, dict):
        out["category"] = category.get("name")
    return out


def _shape(page: object, full: bool) -> object:
    """The light page (`full=True` = Zoho's raw records). The envelope (`count`…)
    is kept: without it the agent thinks it has seen everything."""
    if full or not isinstance(page, dict) or not isinstance(page.get("data"), list):
        return page
    out = dict(page)
    out["data"] = [_slim_article(a) if isinstance(a, dict) else a for a in page["data"]]
    return out


def register(mcp: FastMCP) -> None:
    connector_verify.register("zohodesk", _verify)
    from oto.tools.zohodesk.client import ZohoDeskClient

    def _client() -> ZohoDeskClient:
        creds = access.resolve_credential_fields("zohodesk")
        api_domain, accounts_url = _resolve_dc_domains(creds.get("data_center"))
        return ZohoDeskClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            org_id=creds.get("org_id"),
            api_domain=api_domain,
            accounts_url=accounts_url,
        )

    @mcp.tool()
    def zohodesk_tickets(
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """List support tickets.

        Args:
            status: Open | On Hold | Escalated | Closed.
            sort_by: a field name (prefix with "-" for descending).
        """
        return _client().list_tickets(
            from_index=from_index, limit=limit, department_id=department_id,
            status=status, sort_by=sort_by)

    @mcp.tool()
    def zohodesk_ticket(ticket_id: str, include: Optional[str] = None) -> dict:
        """Get one ticket. `include` = contacts,products,assignee,team…"""
        return _client().get_ticket(ticket_id, include=include)

    @mcp.tool()
    def zohodesk_search_tickets(
        query: dict, from_index: int = 1, limit: int = 50,
    ) -> dict:
        """Search tickets. `query` = dict of field=value pairs (Zoho search params)."""
        return _client().search_tickets(query, from_index=from_index, limit=limit)

    @mcp.tool()
    def zohodesk_create_ticket(data: dict) -> dict:
        """Create a ticket. Required: subject, departmentId, contactId (or contact)."""
        return _client().create_ticket(data)

    @mcp.tool()
    def zohodesk_update_ticket(ticket_id: str, data: dict) -> dict:
        """Patch ticket fields (status, priority, assignee, customFields…)."""
        return _client().update_ticket(ticket_id, data)

    @mcp.tool()
    def zohodesk_ticket_threads(ticket_id: str) -> dict:
        """List the threads (replies/comments) of a ticket."""
        return _client().list_threads(ticket_id)

    @mcp.tool()
    def zohodesk_contacts(from_index: int = 1, limit: int = 50) -> dict:
        """List Desk contacts."""
        return _client().list_contacts(from_index=from_index, limit=limit)

    @mcp.tool()
    def zohodesk_create_contact(data: dict) -> dict:
        """Create a Desk contact. Required: lastName. Optional: firstName, email, phone."""
        return _client().create_contact(data)

    @mcp.tool()
    def zohodesk_departments() -> dict:
        """List Desk departments."""
        return _client().list_departments()

    @mcp.tool()
    def zohodesk_articles(
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        category_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """List Help Center (KB) articles, page by page. To find an article on a
        subject, use `zohodesk_search_articles` instead of paging through the base.

        Each article is a light view: id, title, status, category, summary (Zoho's
        excerpt), modifiedTime, webUrl, portalUrl. The full HTML body is never
        listed: read it with `zohodesk_article`.

        Args:
            limit: 1-50.
            status: Published | Draft | Review | Expired.
            sort_by: a field name (e.g. modifiedTime, viewCount; prefix "-" for desc).
            full: True = Zoho's raw records (owner, author, translations, counters).
        """
        return _shape(_client().list_articles(
            from_index=from_index, limit=limit, department_id=department_id,
            category_id=category_id, status=status, sort_by=sort_by), full)

    @mcp.tool()
    def zohodesk_search_articles(
        query: Optional[str] = None,
        title: Optional[str] = None,
        tag: Optional[str] = None,
        category_id: Optional[str] = None,
        department_id: Optional[str] = None,
        from_index: int = 0,
        limit: int = 10,
        sort_by: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Search Help Center (KB) articles by keyword. Returns `data` (light view
        per article: id, title, status, category, summary — Zoho's excerpt —,
        modifiedTime, webUrl, portalUrl) and `count`, the total number of matches.
        The full HTML body: `zohodesk_article`.

        Args:
            query: words searched across every field of the article (title, body,
                tags…).
            title: searched in the title only.
            tag: searched in the tags only.
            category_id: restricts to one category. At least one of query, title,
                tag or category_id.
            from_index: 0-based start (max 4999).
            limit: 1-100.
            sort_by: relevance | modifiedTime | createdTime (prefix "-" for desc).
            full: True = Zoho's raw records.
        """
        if not (query or title or tag or category_id):
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "Pass at least one search criterion: query (every field), title, tag "
                "or category_id. To browse the whole base, use zohodesk_articles.")))
        return _shape(_client().search_articles(
            query=query, title=title, tag=tag, category_id=category_id,
            department_id=department_id, from_index=from_index, limit=limit,
            sort_by=sort_by), full)

    @mcp.tool()
    def zohodesk_article(article_id: str) -> dict:
        """Get one Help Center article, including its full HTML body (`answer`)."""
        return _client().get_article(article_id)

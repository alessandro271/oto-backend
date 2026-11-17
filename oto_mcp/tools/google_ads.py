"""Google Ads — oto-core surface (GoogleAdsClient) exposed per user, multi-account.

Eighth service of the Google account (2026-10-08): scope `adwords`, granted from its
card. Reads see the Google Ads accounts THEIR Google user can open, nothing more.

**Three tools, the surface of Google's own MCP server** (`googleads/google-ads-mcp`):
- `google_ads_customers` — the accounts the user opens directly;
- `google_ads_search` — one GAQL query;
- `google_ads_fields` — what a resource offers.

**Read-only is enforced by the client** (`oto.tools.google.ads`), not by Google:
`adwords` is the only scope Google publishes and it would allow mutating. The client
builds no mutate URL and refuses anything but a GAQL `SELECT … FROM …` before sending.

**No developer token** (sunset by Google on 2026-09-09): the access level is that of
the Google Cloud project owning the OAuth client. A project on Test access is refused
on real accounts — named refusal, an app setting, never the person's account.

**Paging**: Google pages by 10,000 rows (fixed since v19). The agent reads at most
`max_rows` (≤ 1,000, ~200 KB) per call; the returned `page_token` is OURS — Google's
page token + an offset inside its page — so nothing between two slices is lost.
Google's tokens expire after ~2 h. The Google page behind a cursor is kept a few
minutes in this process (`_PAGES`, per access token): the next slices of the same
page do not ask Google again.

**Operations budget** (`_debit`, 2026-11-17). Every Google request draws on ONE quota:
the Google Cloud project of the platform's OAuth client — Explorer access = 2,880
operations per sliding 24 h for the WHOLE platform, Basic = 15,000. One agent looping
on GAQL would cut every other org (and hammer the audited account). So each Google
request is counted BEFORE it leaves, in the database (`db/platform_key_rate`, shared by
the processes), always enforced — a shared third-party quota, the F-051 class:
- per org, `_ORG_PER_MINUTE` per minute (a loop stops in a minute, not in a day);
- per org, half the platform's daily budget (no org can drain it alone);
- the platform, `OTO_GOOGLE_ADS_DAILY_OPS` per UTC day (default 1,440 = half of
  Explorer's 2,880: a fixed day can double across midnight, the sliding 24 h of
  Google cannot be exceeded; raise it with the access level, `0` = no budget).
A broken counter never refuses: logged, the call goes through. The field catalog
(`google_ads_fields`) is cached per API version: account-independent, it costs once.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Optional

import requests
from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access
from ..auth import google as google_oauth
from ..mcp_errors import McpError

if TYPE_CHECKING:  # the annotation only — never evaluated at runtime
    from oto.tools.google.ads import GoogleAdsClient

_SERVICE = "google_ads"
# Each Google call (connect, read): under the 45 s of a REST invocation, token included.
_HTTP_TIMEOUT = (10, 20)
_TOKEN_TIMEOUT_S = 20
_DEFAULT_ROWS = 200
_MAX_ROWS = 1000
_MAX_OUT_BYTES = 200_000

# Error families (`errorCode` keys) that mean "the request itself is wrong": the
# query, a field, a date — the agent fixes its GAQL, nothing else would help.
_REQUEST_FAMILIES = frozenset({"queryError", "requestError", "fieldError",
                               "fieldMaskError", "dateError", "dateRangeError"})


logger = logging.getLogger(__name__)

#: The named code of a budget refusal — tested by callers, never the sentence.
RATE_CODE = "google_ads_rate_limited"
_ORG_PER_MINUTE = 30
DEFAULT_DAILY_OPS = 1440
_MINUTE_S, _DAY_S = 60, 86_400

# Google pages kept for the next slices of the same page (`_PAGES`).
_PAGE_TTL_S = 600
_PAGE_MAX_ENTRIES = 8
_PAGE_MAX_BYTES = 4_000_000


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


# --- the operations budget ---------------------------------------------------

def daily_ops() -> int:
    """`OTO_GOOGLE_ADS_DAILY_OPS`: Google requests per UTC day for the whole platform;
    absent = 1,440, `0` = no budget. Anything but a whole number ≥ 0 raises."""
    # Name LITERAL: the inventory (`env_inventory.py`) finds its reads by the AST.
    lu = os.environ.get("OTO_GOOGLE_ADS_DAILY_OPS")
    if lu is None or not lu.strip():
        return DEFAULT_DAILY_OPS
    try:
        n = int(lu.strip())
    except ValueError:
        n = -1
    if n < 0:
        raise ValueError(f"OTO_GOOGLE_ADS_DAILY_OPS={lu!r}: unreadable value, expected "
                         "a whole number (0 or more)")
    return n


def verifier_reglages() -> None:
    """Startup guard: an unreadable budget refuses to start, never read as the default."""
    daily_ops()


def _budget_refusal(what: str, used: int, limit: int, window: int) -> McpError:
    wait = int(window - (time.time() % window)) + 1
    when = (f"in {wait} s" if window <= _MINUTE_S
            else f"after 00:00 UTC (in about {wait // 3600 + 1} h)")
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Google Ads read budget reached — {what}: {used} Google requests, "
                 f"limit {limit}. Every Google Ads call of the platform draws on one "
                 f"shared Google quota, so it is capped. Retry {when}; meanwhile send "
                 "fewer, larger queries (ORDER BY … LIMIT, fewer segments, no paging "
                 "through raw rows). Nothing was sent to Google."),
        data={"code": RATE_CODE, "scope": what, "used": used, "limit": limit,
              "window_s": window}))


def _debit(ops: int = 1) -> None:
    """Count `ops` Google requests for the caller's org and for the platform BEFORE
    they leave; past a limit, refuse (always enforced). The counter failing serves."""
    from ..db import platform_key_rate as db_rate

    sub = access.current_user_sub_or_raise()
    org = access.current_org(sub)
    who = f"org:{org}" if org is not None else f"user:{sub}"
    platform = daily_ops()
    checks = [(who, "google_ads:minute", _MINUTE_S, _ORG_PER_MINUTE,
               "this org, per minute")]
    if platform:
        checks += [(who, "google_ads:day", _DAY_S, max(1, platform // 2),
                    "this org, per UTC day"),
                   ("platform", "google_ads:day", _DAY_S, platform,
                    "the whole platform, per UTC day")]
    for scope, key, window, limit, what in checks:
        used = 0
        try:
            for _ in range(ops):
                used = db_rate.compter(scope, key, window)
        except Exception:
            logger.exception("google_ads budget: counter failed for %s on %s — call "
                             "served", scope, key)
            return
        if used > limit:
            if used - ops < limit + 1:   # once per window, not once per call
                logger.warning("google_ads budget: %s crossed %d requests/%ds",
                               scope, limit, window)
            raise _budget_refusal(what, used, limit, window)


# --- what is kept between two calls -------------------------------------------

#: `resource` → its description, for this API version (account-independent).
_FIELDS: dict[tuple[str, str], dict] = {}
_FIELDS_MAX = 300

#: (hashed access token, customer, login customer, query, Google token) → (expires, page).
_PAGES: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()


def _page_get(key: tuple) -> Optional[dict]:
    hit = _PAGES.get(key)
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        _PAGES.pop(key, None)
        return None
    _PAGES.move_to_end(key)
    return hit[1]


def _page_put(key: tuple, data: dict) -> None:
    """Keep a Google page only if a next slice will be read from it, and small enough."""
    rows = data.get("results") or []
    if len(rows) <= _DEFAULT_ROWS:
        return
    if len(json.dumps(rows, default=str, ensure_ascii=False)) > _PAGE_MAX_BYTES:
        return
    _PAGES[key] = (time.monotonic() + _PAGE_TTL_S, data)
    _PAGES.move_to_end(key)
    while len(_PAGES) > _PAGE_MAX_ENTRIES:
        _PAGES.popitem(last=False)


# --- the client, per call ----------------------------------------------------

def _client_for_user(account: Optional[str] = None):
    from oto.tools.google.ads import GoogleAdsClient

    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service=_SERVICE)
    except RuntimeError as e:
        raise _bad(str(e))
    return GoogleAdsClient(creds.token, timeout=_HTTP_TIMEOUT)


async def _client(account: Optional[str] = None) -> GoogleAdsClient:
    """Off the event loop (vault read + possible refresh), with its own deadline."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_TOKEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_TOKEN_TIMEOUT_S}s "
                   "(token refresh) — try again.")


async def _run(fn: Callable[[], Any], ops: int = 1) -> Any:
    """One client read, off the event loop — its `ops` Google requests debited first,
    in the same thread (the counter is SQL); its refusals → what to do next."""
    from oto.tools.google.ads import GoogleAdsError

    def debit_then_read():
        _debit(ops)
        return fn()

    try:
        return await asyncio.to_thread(debit_then_read)
    except GoogleAdsError as e:
        raise _api_error(e)
    except ValueError as e:
        raise _bad(str(e))
    except requests.Timeout:
        raise _bad(f"Google Ads did not answer within {_HTTP_TIMEOUT[1]}s — narrow the "
                   "query (shorter date range, LIMIT) and try again.")
    except requests.RequestException as e:
        raise _bad(f"Google Ads is unreachable ({type(e).__name__}) — try again later.")


def _api_error(e) -> McpError:
    """A `GoogleAdsError` → what to do next, read from the error CODE, not the text."""
    names, detail, http = e.codes, e.detail, e.status_code
    ref = f" (Google request id {e.request_id})" if e.request_id else ""
    if "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION" in names:
        msg = ("This Google Ads connection is only approved for TEST accounts: the Google "
               "Cloud project of the OAuth app that issued it has the « Test » access "
               "level. Reading a real account needs Explorer, Basic or Standard access, "
               "requested by that app's administrator (Google Cloud console → Google Ads "
               "API → Overview). Reconnecting changes nothing.")
    elif "SERVICE_DISABLED" in names:
        msg = ("The Google Ads API is not enabled in the Google Cloud project of the OAuth "
               "app that issued the connection — a setting of that app, for its "
               "administrator (Google Cloud console → APIs & Services → Google Ads API); "
               f"reconnecting changes nothing. Google detail: {e.message}")
    elif "USER_PERMISSION_DENIED" in names:
        msg = ("Your Google user reaches this account through a MANAGER account: pass "
               "`login_customer_id` = the manager's 10-digit id (`google_ads_customers` "
               f"lists the accounts you open directly). Google detail: {detail}")
    elif "NOT_ADS_USER" in names:
        msg = ("This Google account is not a user of any Google Ads account — connect "
               "the Google account that has Google Ads access, or have it invited "
               "(Google Ads → Admin → Access and security).")
    elif "CUSTOMER_NOT_ENABLED" in names:
        msg = (f"This Google Ads account is not enabled (signup unfinished, or cancelled) "
               f"— nothing can be read from it. Google detail: {detail}")
    elif e.families & _REQUEST_FAMILIES or e.status == "INVALID_ARGUMENT":
        msg = (f"Google Ads refused the request: {detail} — check field names and "
               "compatibility with `google_ads_fields`, and the GAQL grammar "
               "(full field names, quoted string values, dates as 'YYYY-MM-DD').")
    elif http == 429 or e.status == "RESOURCE_EXHAUSTED":
        msg = f"Google Ads: quota or rate limit reached — try again later. Detail: {detail}"
    elif http == 401:
        msg = ("Google refused the access token for Google Ads — reconnect Google Ads "
               f"from its card. Google detail: {e.message}")
    elif http == 403:
        msg = (f"Your Google user has no access to this Google Ads account: {detail} — "
               "check the customer id, or pass the manager's `login_customer_id`.")
    elif http == 404:
        msg = f"Google Ads cannot find this resource: {detail}"
    elif http >= 500:
        msg = f"Google Ads is temporarily unavailable (HTTP {http}) — try again later."
    else:
        msg = f"Google Ads refused the request (HTTP {http}, {e.status or '?'}): {detail}"
    return _bad(msg + ref)


# --- our cursor over Google's 10,000-row pages -------------------------------

def _query_key(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _encode_cursor(google_token: Optional[str], offset: int, query: str) -> str:
    raw = json.dumps({"g": google_token, "o": offset, "q": _query_key(query)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, query: str) -> tuple[Optional[str], int]:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        google_token, offset, key = data["g"], int(data["o"]), data["q"]
    except (ValueError, KeyError, TypeError):
        raise _bad("`page_token` is not one returned by `google_ads_search`.") from None
    if key != _query_key(query) or offset < 0:
        raise _bad("`page_token` belongs to another query: resend the SAME query "
                   "with it, or omit it to start over.")
    return google_token, offset


def _page(data: dict, query: str, google_token: Optional[str], offset: int,
          max_rows: int) -> dict:
    """A Google page → the slice returned to the agent, and the cursor that follows."""
    from oto.tools.google.ads import flatten_rows

    try:
        columns, table = flatten_rows(data)
    except ValueError as e:
        raise _bad(str(e))
    rows: list[list] = []
    size = 0
    truncated_bytes = False
    for line in table[offset:offset + max_rows]:
        size += len(json.dumps(line, default=str, ensure_ascii=False)) + 1
        if size > _MAX_OUT_BYTES and rows:
            truncated_bytes = True
            break
        rows.append(line)
    out: dict = {"columns": columns, "rows": rows, "row_count": len(rows)}
    nxt = offset + len(rows)
    if nxt < len(table):
        out["page_token"] = _encode_cursor(google_token, nxt, query)
    elif data.get("nextPageToken"):
        out["page_token"] = _encode_cursor(data["nextPageToken"], 0, query)
    if truncated_bytes:
        out["truncated_bytes"] = True
        out["hint"] = (f"page cut at ~{_MAX_OUT_BYTES // 1000} KB — select fewer fields, "
                       "or aggregate (segments.date out, a coarser segment).")
    elif "page_token" in out:
        out["hint"] = ("more rows — call again with the SAME query and `page_token` "
                       "(valid ~2 h), or narrow with WHERE / LIMIT.")
    return out


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def google_ads_customers(account: Optional[str] = None) -> dict:
        """List the Google Ads accounts your Google user opens DIRECTLY (customer ids).

        Start here when no customer id is known. A manager account (MCC) appears as
        one id: its client accounts are read with `google_ads_search` on the manager —
        `SELECT customer_client.id, customer_client.descriptive_name,
        customer_client.manager, customer_client.status FROM customer_client` — then
        each client is queried with `login_customer_id` = the manager's id. An
        account's name, currency and time zone: `SELECT customer.descriptive_name,
        customer.currency_code, customer.time_zone FROM customer`.

        Returns {customer_ids: ["1234567890", …]}.

        Args:
            account: email of the Google account to use — same choice as `_account`.
        """
        client = await _client(account)
        return {"customer_ids": await _run(client.list_accessible_customers)}

    @mcp.tool()
    async def google_ads_search(
        customer_id: str,
        query: str,
        max_rows: int = _DEFAULT_ROWS,
        page_token: Optional[str] = None,
        login_customer_id: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Run one read-only GAQL query (Google Ads Query Language) on a Google Ads account.

        GAQL is `SELECT <fields> FROM <resource> [WHERE …] [ORDER BY …] [LIMIT n]`:
        full field names (`campaign.name`, never `name`), no joins, no `*`. Look up
        what a resource offers with `google_ads_fields` — do not guess fields.
        Money is in MICROS of the account currency (`metrics.cost_micros` / 1e6).
        Dates are in the account time zone. The answer names fields in camelCase
        (`metrics.cost_micros` → column `metrics.costMicros`), 64-bit numbers (ids,
        clicks, micros) come as strings, and a field Google leaves out of a row is
        null. Typical queries —
        - campaigns over 30 days: `SELECT campaign.id, campaign.name,
          campaign.status, metrics.cost_micros, metrics.impressions, metrics.clicks,
          metrics.conversions, metrics.conversions_value FROM campaign WHERE
          segments.date DURING LAST_30_DAYS ORDER BY metrics.cost_micros DESC`
        - daily spend: add `segments.date` to the SELECT (one row per day);
        - ads: `SELECT ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type,
          ad_group_ad.ad.final_urls, ad_group_ad.status, metrics.clicks FROM
          ad_group_ad WHERE segments.date DURING LAST_7_DAYS`
        - keywords: `SELECT ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type, metrics.clicks, metrics.cost_micros
          FROM keyword_view WHERE segments.date DURING LAST_30_DAYS`
        - search terms: `… FROM search_term_view WHERE segments.date DURING
          LAST_7_DAYS`.
        Date ranges: `DURING LAST_7_DAYS | LAST_30_DAYS | THIS_MONTH | LAST_MONTH`,
        or `segments.date BETWEEN '2026-01-01' AND '2026-01-31'`. More:
        oto_guide op=read slug="google-ads-gaql".

        Returns {columns (the selected fields), rows (lists, in column order),
        row_count, page_token?, hint?} — at most `max_rows` rows per
        call; more → call again with the SAME query and `page_token`.

        Args:
            customer_id: the Google Ads account, 10 digits (dashes accepted).
            query: the GAQL query (SELECT only).
            max_rows: rows returned in this call (default 200, max 1000).
            page_token: `page_token` of the previous call, same query.
            login_customer_id: the manager account (10 digits) through which you
                reach `customer_id`, when your access goes through a manager.
            account: email of the Google account to use — same choice as `_account`.
        """
        from oto.tools.google.ads import check_select, customer_id as normalize

        try:
            cid = normalize(customer_id)
            lcid = normalize(login_customer_id) if login_customer_id else None
            query = check_select(query)
        except ValueError as e:
            raise _bad(str(e))
        if max_rows < 1:
            raise _bad("max_rows must be ≥ 1.")
        google_token, offset = (_decode_cursor(page_token, query) if page_token
                                else (None, 0))
        client = await _client(account)
        # The next slices of a Google page come from `_PAGES`: no second request. Keyed
        # by the ACCESS TOKEN that would ask (hashed): the same Google identity, and
        # its current grant — another account, a reconnection, a new token: no hit.
        key = (hashlib.sha256(str(getattr(client, "_token", "")).encode()).hexdigest(),
               cid, lcid, query, google_token)
        data = _page_get(key) if offset else None
        if data is None:
            data = await _run(lambda: client.search(cid, query, page_token=google_token,
                                                    login_customer_id=lcid))
            _page_put(key, data)
        return {"customer_id": cid,
                **_page(data, query, google_token, offset, min(max_rows, _MAX_ROWS))}

    @mcp.tool()
    async def google_ads_fields(resource: str, account: Optional[str] = None) -> dict:
        """What a Google Ads resource offers to GAQL: its fields, and the metrics,
        segments and related-resource fields selectable WITH it.

        Use it before writing a `google_ads_search` query on a resource you have not
        queried yet (`campaign`, `ad_group`, `ad_group_ad`, `keyword_view`,
        `search_term_view`, `customer`, `customer_client`…). The answer changes only
        with Google's API version: reuse it within a conversation.

        Returns {resource, attributes, metrics, segments, related, not_filterable,
        not_sortable} — every list holds full field names, the first four are the
        selectable ones.

        Args:
            resource: the resource name, snake_case (e.g. "campaign", "ad_group_ad").
            account: email of the Google account to use — same choice as `_account`.
        """
        from oto.tools.google.ads import API_VERSION

        cle = (API_VERSION, (resource or "").strip())
        if cle in _FIELDS:
            return _FIELDS[cle]
        client = await _client(account)
        try:
            # Two `googleAdsFields:search` requests (its own fields, then the linked).
            out = await _run(lambda: client.describe_resource(resource), ops=2)
        except RuntimeError as e:
            raise _bad(str(e))
        if len(_FIELDS) < _FIELDS_MAX:
            _FIELDS[cle] = out
        return out

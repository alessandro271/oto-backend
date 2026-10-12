"""DataForSEO — SEO data: keywords, domains, backlinks (dataforseo.com).

Wraps `oto.tools.dataforseo.DataForSeoClient` (API v3, live endpoints only).
Credential = API login + API password (`secret_kind="fields"`); byo user/org
and an OPEN platform key with a monthly call quota (providers/dataforseo.py).

Four tools, one per family (ADR 0047), each with an `op`:
- `dataforseo_keywords`: volume, CPC, difficulty, intent, ideas, suggestions.
- `dataforseo_domain`: a site's rankings, traffic, pages, competitors, keyword gap.
- `dataforseo_backlinks`: link profile summary, backlinks, referring domains, anchors.
- `dataforseo_account`: the balance of YOUR key, the location/language codes.

Versus its neighbours: `serper_search` is the raw Google results page (where a
site ranks for one query, today) — DataForSEO is the metrics around it.
`ahrefs_*` and `ubersuggest_*` serve the same questions from the customer's own
seat on those products.

**Billing.** Every task reports its real USD `cost`; the call's trace gets it as
`quantity` in micro-dollars, like `jev`. An upstream refusal is not charged.

**Default view.** Results are trimmed: nulls dropped and the heavy per-month /
SERP / breakdown blocks removed (`_HEAVY`). `full=True` returns DataForSEO's
result as is. Lists default to `limit=20` (billed per returned row).

Client calls are written in plain sight (`c.keyword_overview(…)`), not dispatched
by name: that is what keeps them checkable by the version-skew probe.
"""
from __future__ import annotations

import math
import re
from contextlib import contextmanager
from typing import Any, Optional, Union

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..connectors import verify as connector_verify

_LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")
_LIMIT_DEFAULT = 20
_LIMIT_MAX = 1000
_LOCATIONS_MAX = 50

# Blocks dropped from the default view, at any depth: per-month series, SERP
# snapshots, alternative normalisations and top-N breakdowns. All kept by `full=True`.
_HEAVY = frozenset({
    "monthly_searches", "serp_info", "keyword_info_normalized_with_bing",
    "keyword_info_normalized_with_clickstream", "clickstream_keyword_info",
    "clickstream_etv", "clickstream_gender_distribution",
    "clickstream_age_distribution", "avg_backlinks_info", "impressions_info",
    "search_volume_trend", "categories", "metrics_absolute", "se_type",
    "referring_links_tld", "referring_links_types", "referring_links_attributes",
    "referring_links_platform_types", "referring_links_semantic_locations",
    "referring_links_countries", "monthly_searches_history",
})

# Location / language the API REQUIRES per op (spec, live endpoints). Checked here
# so the agent gets the fix in one message instead of a numeric API refusal.
_NEEDS_LOCATION = {("keywords", "overview"), ("keywords", "ideas"),
                   ("keywords", "related"), ("keywords", "difficulty"),
                   ("domain", "competitors"), ("domain", "gap"),
                   ("domain", "relevant_keywords")}
_NEEDS_LANGUAGE = {("keywords", "overview"), ("keywords", "related"),
                   ("keywords", "difficulty"), ("domain", "competitors"),
                   ("domain", "gap")}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _microdollars(cost: Optional[float]) -> int:
    """Call cost in micro-dollars, rounded UP; no declared cost bills 0. The
    `round` before `ceil` keeps a float like 1.0000000000000002e-05 at 10 µ$."""
    return math.ceil(round(float(cost) * 1e6, 6)) if cost else 0


def _creds(fields: dict, config: Optional[dict] = None) -> tuple[str, str]:
    """`(login, password)` from the stored fields. A platform key is posted as ONE
    value (`login:password`), which lands in a single field: split it there."""
    f = {**(config or {}), **(fields or {})}
    login, password = f.get("login"), f.get("password")
    if not (login and password):
        for v in (password, login, f.get("key"), f.get("api_key")):
            if isinstance(v, str) and ":" in v:
                login, _, password = v.partition(":")
                break
    if not (login and password):
        raise _bad("The DataForSEO credential needs both the API login and the API "
                   "password (a shared key is entered as `login:password`).")
    return login.strip(), password


def _upstream_message(e, is_platform: bool) -> str:
    status = e.status_code
    detail = e.body.get("status_message") if isinstance(e.body, dict) else e.body
    if status == 403:
        # e.g. 40104, an account not yet verified: DataForSEO's own words say what to do.
        return f"DataForSEO refused access (HTTP 403): {detail}"
    if status == 401:
        return ("DataForSEO rejected the API login/password (HTTP 401) — check the "
                "credential on this connector (DataForSEO dashboard → API Access; the "
                "API password is not the sign-in password).")
    if status == 402:
        if is_platform:
            return ("The shared DataForSEO balance is empty — tell your platform admin, "
                    "or set your own DataForSEO key on this connector.")
        return "DataForSEO: the account balance is empty — top it up at app.dataforseo.com."
    if status == 429:
        return "DataForSEO: too many requests — try again in a minute."
    if status == 404:
        return f"DataForSEO: not found — {detail}"
    if status == 400:
        return f"DataForSEO refused the request: {detail}"
    if status >= 500:
        return f"DataForSEO is temporarily unavailable ({detail}) — try again later."
    return f"DataForSEO refused the request (HTTP {status}): {detail}"


def _loc(location: Optional[Union[int, str]], language: Optional[str]) -> dict:
    """`location`: a DataForSEO location code (2250 = France, 2840 = United States)
    or its English name; `language`: an ISO code (`fr`) or its English name."""
    out: dict = {}
    if location is not None and location != "":
        if isinstance(location, int) or str(location).strip().isdigit():
            out["location_code"] = int(location)
        else:
            out["location_name"] = str(location).strip()
    if language:
        lang = language.strip()
        out["language_code" if _LANG_CODE.match(lang) else "language_name"] = lang
    return out


def _slim(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items()
                if v is not None and k not in _HEAVY}
    if isinstance(value, list):
        return [_slim(v) for v in value]
    return value


def _view(task: dict, full: bool, flat: bool = False) -> dict:
    """The answer: the call's USD cost, then the result — trimmed unless `full`.
    `flat`: the result IS the list of rows (Google Ads volume), so it always
    lands in `items`, one keyword or many."""
    result = task.get("result") or []
    out: dict = {"cost_usd": task.get("cost")}
    if full:
        out["result"] = result
        return out
    if not flat and len(result) == 1 and isinstance(result[0], dict):
        out.update(_slim(result[0]))
    else:
        out["items"] = _slim(result)
    return out


def _limit(limit: Optional[int]) -> int:
    if limit is None:
        return _LIMIT_DEFAULT
    if not 1 <= int(limit) <= _LIMIT_MAX:
        raise _bad(f"limit must be between 1 and {_LIMIT_MAX}.")
    return int(limit)


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe: `GET /appendix/user_data`, free."""
    from oto.tools.dataforseo import DataForSeoClient
    login, password = _creds(fields, config)
    DataForSeoClient(login=login, password=password).user_data()


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.dataforseo import DataForSeoClient

    connector_verify.register("dataforseo", _verify)

    def _client() -> tuple[DataForSeoClient, bool]:
        rc = access.resolve_credential("dataforseo", want="auto")
        login, password = _creds(rc.fields, rc.config)
        return DataForSeoClient(login=login, password=password), rc.is_platform

    @contextmanager
    def _upstream(is_platform: bool):
        try:
            yield
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, is_platform))
        except ValueError as e:
            raise _bad(str(e))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise _bad(f"DataForSEO unreachable (network/timeout) — try again later. {e}")

    def _bill(task: dict) -> None:
        u = _microdollars(task.get("cost"))
        if u:
            session_org.note_call_trace(quantity=u)

    def _need(value, name: str, op: str):
        if value is None or value == "" or value == []:
            raise _bad(f"op='{op}' requires `{name}`.")
        return value

    def _refuse(op: str, **given) -> None:
        """An argument this op does not use is refused, never dropped silently."""
        for name, value in given.items():
            if value is not None and value != "" and value != []:
                raise _bad(f"op='{op}' does not use `{name}`.")

    def _check_place(family: str, op: str, loc: dict) -> None:
        if (family, op) in _NEEDS_LOCATION and not (
                "location_code" in loc or "location_name" in loc):
            raise _bad(f"op='{op}' requires `location` (e.g. 2250 for France, 2840 for "
                       "the United States, or the English name; codes: "
                       "dataforseo_account(op='locations')).")
        if (family, op) in _NEEDS_LANGUAGE and not (
                "language_code" in loc or "language_name" in loc):
            raise _bad(f"op='{op}' requires `language` (e.g. 'fr', 'en').")

    # --- keywords -----------------------------------------------------------

    @mcp.tool()
    def dataforseo_keywords(
        op: str,
        keywords: Optional[list[str]] = None,
        keyword: Optional[str] = None,
        location: Optional[Union[int, str]] = None,
        language: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        depth: Optional[int] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Keyword research on Google: volume, CPC, difficulty, intent, and ideas.

        Ops (billed per call, plus per returned row on list ops):
        - `overview` — `keywords` (≤700): volume, CPC, competition, difficulty,
          intent. `location` + `language` required.
        - `volume` — `keywords` (≤1000): Google Ads (Keyword Planner) volume, CPC,
          competition. No `location` = worldwide.
        - `difficulty` — `keywords` (≤1000): ranking difficulty 0-100.
          `location` + `language` required.
        - `intent` — `keywords` (≤1000): informational / navigational / commercial /
          transactional, with a probability.
        - `ideas` — `keywords` (≤200 seeds): keywords of the same topic.
          `location` required.
        - `suggestions` — `keyword`: long-tail queries containing it.
        - `related` — `keyword`: Google's "related searches", `depth` 0-4 levels.
          `location` + `language` required.

        Returns — `{cost_usd, …, items: [{keyword, keyword_info: {search_volume,
        cpc, competition}, keyword_properties: {keyword_difficulty},
        search_intent_info}]}` (trimmed; `full=True` for the raw result).

        Args:
            op: see above.
            keywords / keyword: the input, per op.
            location: location code (2250 France, 2840 United States, 2826 United
                Kingdom) or English name; list: `dataforseo_account(op="locations")`.
            language: ISO code (`fr`, `en`) or English name.
            limit: rows for list ops (default 20, max 1000) — each row is billed.
            offset: skip that many rows.
            filters: DataForSEO filter array, e.g.
                `["keyword_info.search_volume", ">", 100]`, combined with "and"/"or".
            order_by: e.g. `["keyword_info.search_volume,desc"]`.
            depth: `related` only.
            options: any other documented field of the endpoint, as is
                (e.g. `{"include_serp_info": true}`).
            full: return DataForSEO's result untrimmed.
        """
        loc = _loc(location, language)
        _check_place("keywords", op, loc)
        c, is_platform = _client()
        lists = dict(limit=_limit(limit), offset=offset, filters=filters, order_by=order_by)
        with _upstream(is_platform):
            if op in ("overview", "volume", "difficulty", "intent"):
                _refuse(op, keyword=keyword, depth=depth, offset=offset,
                        filters=filters, order_by=order_by,
                        limit=limit)
                _need(keywords, "keywords", op)
                if op == "overview":
                    task = c.keyword_overview(keywords, options=options, **loc)
                elif op == "volume":
                    task = c.google_ads_search_volume(keywords, options=options, **loc)
                elif op == "difficulty":
                    _refuse(op, options=options)
                    task = c.bulk_keyword_difficulty(keywords, **loc)
                else:
                    _refuse(op, options=options, location=location)
                    task = c.search_intent(keywords, language_code=loc.get("language_code"),
                                           language_name=loc.get("language_name"))
            elif op == "ideas":
                _refuse(op, keyword=keyword, depth=depth)
                task = c.keyword_ideas(_need(keywords, "keywords", op), options=options,
                                       **lists, **loc)
            elif op == "suggestions":
                _refuse(op, keywords=keywords, depth=depth)
                task = c.keyword_suggestions(_need(keyword, "keyword", op), options=options,
                                             **lists, **loc)
            elif op == "related":
                _refuse(op, keywords=keywords)
                task = c.related_keywords(_need(keyword, "keyword", op), depth=depth,
                                          options=options, **lists, **loc)
            else:
                raise _bad("op must be one of: overview, volume, difficulty, intent, "
                           "ideas, suggestions, related.")
        _bill(task)
        return _view(task, full, flat=op == "volume")

    # --- domains ------------------------------------------------------------

    @mcp.tool()
    def dataforseo_domain(
        op: str,
        target: Optional[str] = None,
        target2: Optional[str] = None,
        targets: Optional[list[str]] = None,
        location: Optional[Union[int, str]] = None,
        language: Optional[str] = None,
        shared_only: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """A website's organic search footprint on Google, from DataForSEO Labs.

        `target` is a domain WITHOUT `https://` or `www.` (`example.com`); `keywords`
        also takes a page URL.

        Ops:
        - `overview` — ranking keyword count per position bucket, estimated
          monthly traffic (`etv`) and its paid value.
        - `keywords` — the keywords the domain or page ranks for, with position,
          URL and volume (sorted as `order_by`, e.g. `["keyword_data.keyword_info.search_volume,desc"]`).
        - `pages` — its pages with their keyword counts and estimated traffic.
        - `competitors` — domains ranking for the same keywords. `location` +
          `language` required.
        - `gap` — with `target2`: the keywords both rank for (`shared_only=True`,
          default), or those `target` ranks for and `target2` does not
          (`shared_only=False`). `location` + `language` required.
        - `relevant_keywords` — keywords relevant to the site's content, ranked or
          not. `location` required.
        - `traffic` — `targets` (≤1000 domains or pages): estimated organic and
          paid traffic of each, in one call.

        Returns — `{cost_usd, total_count?, items: [...]}`, trimmed (`full=True`
        for the raw result). Estimates are DataForSEO's, not the site's analytics.

        Args:
            op: see above.
            target / target2 / targets: per op.
            location / language: as in `dataforseo_keywords`; no location = all.
            shared_only: `gap` only.
            limit: rows (default 20, max 1000) — each row is billed.
            offset, filters, order_by, options, full: as in `dataforseo_keywords`.
        """
        loc = _loc(location, language)
        _check_place("domain", op, loc)
        c, is_platform = _client()
        lists = dict(limit=_limit(limit), offset=offset, filters=filters, order_by=order_by)
        with _upstream(is_platform):
            if op == "traffic":
                _refuse(op, target=target, target2=target2, shared_only=shared_only,
                        limit=limit, offset=offset, filters=filters, order_by=order_by)
                task = c.bulk_traffic_estimation(_need(targets, "targets", op),
                                                 options=options, **loc)
            elif op == "overview":
                _refuse(op, target2=target2, targets=targets, shared_only=shared_only,
                        limit=limit, offset=offset, filters=filters, order_by=order_by)
                task = c.domain_rank_overview(_need(target, "target", op),
                                              options=options, **loc)
            elif op == "gap":
                _refuse(op, targets=targets)
                task = c.domain_intersection(
                    _need(target, "target", op), _need(target2, "target2", op),
                    intersections=True if shared_only is None else shared_only,
                    options=options, **lists, **loc)
            else:
                _refuse(op, target2=target2, targets=targets, shared_only=shared_only)
                t = _need(target, "target", op)
                if op == "keywords":
                    task = c.ranked_keywords(t, options=options, **lists, **loc)
                elif op == "pages":
                    task = c.relevant_pages(t, options=options, **lists, **loc)
                elif op == "competitors":
                    task = c.competitors_domain(t, options=options, **lists, **loc)
                elif op == "relevant_keywords":
                    task = c.keywords_for_site(t, options=options, **lists, **loc)
                else:
                    raise _bad("op must be one of: overview, keywords, pages, "
                               "competitors, gap, relevant_keywords, traffic.")
        _bill(task)
        return _view(task, full)

    # --- backlinks ----------------------------------------------------------

    @mcp.tool()
    def dataforseo_backlinks(
        op: str,
        target: Optional[str] = None,
        targets: Optional[list[str]] = None,
        mode: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Backlink profile of a domain, subdomain or page (DataForSEO index).

        `target`: a domain or subdomain WITHOUT `https://` or `www.`, or a page's
        absolute URL.

        Ops:
        - `summary` — totals: backlinks, referring domains, rank, spam score.
        - `list` — the backlinks (source URL, anchor, dofollow, rank, first/last
          seen); `mode`: `as_is` (default) | `one_per_domain` | `one_per_anchor`.
        - `referring_domains` — the domains linking to the target.
        - `anchors` — anchor texts pointing to the target.
        - `ranks` — `targets` (≤1000): the backlink rank of each, in one call.

        Returns — `{cost_usd, …, items: [...]}`, trimmed (`full=True` for the raw
        result, including the TLD / country / link-type breakdowns of `summary`).

        Args:
            op: see above.
            target / targets: per op.
            mode: `list` only.
            limit: rows (default 20, max 1000) — each row is billed.
            offset, filters, order_by, full: as in `dataforseo_keywords`.
            options: other documented fields, e.g. `{"include_subdomains": false}`,
                `{"backlinks_status_type": "live"}`.
        """
        c, is_platform = _client()
        with _upstream(is_platform):
            if op == "ranks":
                _refuse(op, target=target, mode=mode, limit=limit, offset=offset,
                        filters=filters, order_by=order_by)
                task = c.bulk_ranks(_need(targets, "targets", op), options=options)
            elif op == "summary":
                _refuse(op, targets=targets, mode=mode, limit=limit, offset=offset,
                        filters=filters, order_by=order_by)
                task = c.backlinks_summary(_need(target, "target", op), options=options)
            else:
                _refuse(op, targets=targets)
                t = _need(target, "target", op)
                lists = dict(limit=_limit(limit), offset=offset, filters=filters,
                             order_by=order_by, options=options)
                if op == "list":
                    task = c.backlinks_list(t, mode=mode, **lists)
                elif op == "referring_domains":
                    _refuse(op, mode=mode)
                    task = c.referring_domains(t, **lists)
                elif op == "anchors":
                    _refuse(op, mode=mode)
                    task = c.anchors(t, **lists)
                else:
                    raise _bad("op must be one of: summary, list, referring_domains, "
                               "anchors, ranks.")
        _bill(task)
        return _view(task, full)

    # --- account ------------------------------------------------------------

    @mcp.tool()
    def dataforseo_account(op: str = "balance", query: Optional[str] = None) -> dict:
        """Your DataForSEO account, and the location codes the other tools take. Free.

        Ops:
        - `balance` — the remaining USD balance of YOUR key (not available on the
          shared key: that balance is the platform's).
        - `locations` — location codes and their languages; `query` filters by
          name (e.g. "France", "Paris"), first 50 matches.

        Returns — `balance`: `{login, balance_usd, total_paid_usd}`;
        `locations`: `{count, items: [{location_code, location_name,
        location_type, country_iso_code, languages: [{language_name, language_code}]}]}`.
        """
        c, is_platform = _client()
        with _upstream(is_platform):
            if op == "balance":
                _refuse(op, query=query)
                if is_platform:
                    raise _bad("op='balance' reads YOUR DataForSEO key; this call runs "
                               "on the shared key, whose balance is the platform's.")
                task = c.user_data()
                data = (task.get("result") or [{}])[0] or {}
                money = data.get("money") or {}
                return {"login": data.get("login"), "balance_usd": money.get("balance"),
                        "total_paid_usd": money.get("total")}
            if op == "locations":
                task = c.labs_locations_and_languages()
                q = (query or "").strip().lower()
                rows = [r for r in (task.get("result") or [])
                        if not q or q in str(r.get("location_name", "")).lower()]
                return {"count": len(rows), "items": [{
                    "location_code": r.get("location_code"),
                    "location_name": r.get("location_name"),
                    "location_type": r.get("location_type"),
                    "country_iso_code": r.get("country_iso_code"),
                    "languages": [{"language_name": lg.get("language_name"),
                                   "language_code": lg.get("language_code")}
                                  for lg in (r.get("available_languages") or [])],
                } for r in rows[:_LOCATIONS_MAX]]}
            raise _bad("op must be `balance` or `locations`.")

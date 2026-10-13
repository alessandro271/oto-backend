"""DataForSEO — SEO data: keywords, domains, backlinks (dataforseo.com).

Wraps `oto.tools.dataforseo.DataForSeoClient` (API v3). Credential = API login +
API password (`secret_kind="fields"`); byo user/org and a paid platform key
posted closed (providers/dataforseo.py).

Eight tools over three modules (`providers/dataforseo.py::modules`), one per
family (ADR 0047), each with an `op`:
- here: `dataforseo_keywords` (volume, CPC, difficulty, intent, ideas, Google
  Trends), `dataforseo_domain` (rankings, traffic and its history, pages,
  subdomains, competitors, keyword gap), `dataforseo_backlinks` (profile, links,
  referring domains, anchors, history, new/lost, link gap), `dataforseo_account`;
- `dataforseo_insights.py`: `dataforseo_ai_visibility` (brand mentions in AI
  answers, asking a model, Google AI Mode) and `dataforseo_web_mentions`;
- `dataforseo_site.py`: `dataforseo_site_audit` (one page, Lighthouse, site
  crawl) and `dataforseo_technologies` (a site's stack, sites using a technology).
This module also holds what the three share: credential, refusals, view, billing.

Versus its neighbours: `serper_search` is the raw Google results page (where a
site ranks for one query, today) — DataForSEO is the metrics around it.
`ahrefs_*` and `ubersuggest_*` serve the same questions from the customer's own
seat on those products.

**Billing.** Every task reports its real USD `cost`; the call's trace gets it as
`quantity` in micro-dollars, like `jev` (0 for a free read). An upstream refusal
is not charged.

**Default view.** Results are trimmed: nulls dropped and the heavy per-month /
SERP / breakdown blocks removed (`_HEAVY`). `full=True` returns DataForSEO's
result as is. Lists default to `limit=20` (billed per returned row).

Client calls are written in plain sight (`c.keyword_overview(…)`), not dispatched
by name: that is what keeps them checkable by the version-skew probe.
"""
from __future__ import annotations

import datetime as _dt
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
    "referring_links_countries", "monthly_searches_history", "ai_monthly_searches",
    "check_url", "xpath", "crawl_gateway_address", "custom_js_response",
    "custom_js_client_exception",
})

# Location / language the API REQUIRES per op (spec, checked live). Checked here so
# the agent gets the fix in one message instead of a numeric API refusal.
_NEEDS_LOCATION = {("keywords", "overview"), ("keywords", "ideas"),
                   ("keywords", "related"), ("keywords", "difficulty"),
                   ("keywords", "ai_volume"),
                   ("domain", "competitors"), ("domain", "gap"),
                   ("domain", "relevant_keywords"), ("domain", "history"),
                   ("domain", "serp_competitors"), ("ai", "ai_mode")}
_NEEDS_LANGUAGE = {("keywords", "overview"), ("keywords", "related"),
                   ("keywords", "difficulty"), ("keywords", "ai_volume"),
                   ("domain", "competitors"), ("domain", "gap"), ("domain", "history"),
                   ("domain", "serp_competitors"), ("ai", "ai_mode")}


def bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _microdollars(cost: Optional[float]) -> int:
    """Call cost in micro-dollars, rounded UP; no declared cost bills 0. The
    `round` before `ceil` keeps a float like 1.0000000000000002e-05 at 10 µ$."""
    return math.ceil(round(float(cost) * 1e6, 6)) if cost else 0


def bill(task: dict) -> None:
    """The call's real spend on its trace — 0 for a free read, never left NULL
    (the ledger reads a NULL quantity as 1)."""
    session_org.note_call_trace(quantity=_microdollars(task.get("cost")))


def creds(fields: dict, config: Optional[dict] = None) -> tuple[str, str]:
    """`(login, password)` from the stored fields — both are asked at posting, the
    platform key included. A single `login:password` value is NOT split: a credential
    posted wrong is refused by name, never guessed into shape."""
    f = {**(config or {}), **(fields or {})}
    login, password = f.get("login"), f.get("password")
    if not (login and password):
        raise bad("The DataForSEO credential needs both the API login and the API "
                  "password.")
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


@contextmanager
def upstream(is_platform: bool):
    """Translates a DataForSEO refusal into an actionable tool error."""
    from oto.tools.common.errors import UpstreamHTTPError
    try:
        yield
    except UpstreamHTTPError as e:
        raise bad(_upstream_message(e, is_platform))
    except ValueError as e:
        raise bad(str(e))
    except (requests.ConnectionError, requests.Timeout) as e:
        raise bad(f"DataForSEO unreachable (network/timeout) — try again later. {e}")


def resolve() -> tuple[str, str, bool]:
    """`(login, password, is_platform)` of the winning credential."""
    rc = access.resolve_credential("dataforseo", want="auto")
    login, password = creds(rc.fields, rc.config)
    return login, password, rc.is_platform


def present(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def need(value, name: str, op: str):
    if not present(value):
        raise bad(f"op='{op}' requires `{name}`.")
    return value


def refuse(op: str, **given) -> None:
    """An argument this op does not use is refused, never dropped silently."""
    for name, value in given.items():
        if present(value):
            raise bad(f"op='{op}' does not use `{name}`.")


def loc(location: Optional[Union[int, str]], language: Optional[str]) -> dict:
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


def check_place(family: str, op: str, place: dict) -> None:
    if (family, op) in _NEEDS_LOCATION and not (
            "location_code" in place or "location_name" in place):
        raise bad(f"op='{op}' requires `location` (e.g. 2250 for France, 2840 for the "
                  "United States, or the English name; codes: "
                  "dataforseo_account(op='locations')).")
    if (family, op) in _NEEDS_LANGUAGE and not (
            "language_code" in place or "language_name" in place):
        raise bad(f"op='{op}' requires `language` (e.g. 'fr', 'en').")


def slim(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: slim(v) for k, v in value.items()
                if v is not None and k not in _HEAVY}
    if isinstance(value, list):
        return [slim(v) for v in value]
    return value


def view(task: dict, full: bool, flat: bool = False) -> dict:
    """The answer: the call's USD cost, then the result — trimmed unless `full`.
    `flat`: the result IS the list of rows (Google Ads volume), so it always
    lands in `items`, one keyword or many."""
    result = task.get("result") or []
    out: dict = {"cost_usd": task.get("cost")}
    if full:
        out["result"] = result
        return out
    if not flat and len(result) == 1 and isinstance(result[0], dict):
        out.update(slim(result[0]))
    else:
        out["items"] = slim(result)
    return out


def limit_of(limit: Optional[int], default: int = _LIMIT_DEFAULT,
             maximum: int = _LIMIT_MAX, name: str = "limit") -> int:
    if limit is None:
        return default
    if not 1 <= int(limit) <= maximum:
        raise bad(f"{name} must be between 1 and {maximum}.")
    return int(limit)


def yesterday() -> str:
    """Default end of a backlink time range: DataForSEO refuses a range left
    open-ended (`date_to` omitted) on some endpoints."""
    return (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)).isoformat()


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe: `GET /appendix/user_data`, free."""
    from oto.tools.dataforseo import DataForSeoClient
    login, password = creds(fields, config)
    DataForSeoClient(login=login, password=password).user_data()


def register(mcp: FastMCP) -> None:
    from oto.tools.dataforseo import DataForSeoClient

    connector_verify.register("dataforseo", _verify)

    def _client() -> tuple[DataForSeoClient, bool]:
        login, password, is_platform = resolve()
        return DataForSeoClient(login=login, password=password), is_platform

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
        time_range: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Keyword research on Google: volume, CPC, difficulty, intent, ideas, trends.

        Ops (billed per call, plus per returned row on list ops; ≈$0.01 each):
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
        - `trends` — `keywords` (≤5): Google Trends interest over time, compared
          (0-100). `time_range` (`past_12_months`, `past_5_years`…) or dates.
        - `ai_volume` — `keywords` (≤1000): how often each is asked to AI
          assistants. `location` + `language` required.

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
            time_range / date_from / date_to: `trends` only (dates `yyyy-mm-dd`).
            options: any other documented field of the endpoint, as is
                (e.g. `{"include_serp_info": true}`).
            full: return DataForSEO's result untrimmed.
        """
        place = loc(location, language)
        check_place("keywords", op, place)
        if op != "trends":
            refuse(op, time_range=time_range, date_from=date_from, date_to=date_to)
        c, is_platform = _client()
        lists = dict(limit=limit_of(limit), offset=offset, filters=filters, order_by=order_by)
        with upstream(is_platform):
            if op in ("overview", "volume", "difficulty", "intent", "trends", "ai_volume"):
                refuse(op, keyword=keyword, depth=depth, offset=offset,
                       filters=filters, order_by=order_by, limit=limit)
                need(keywords, "keywords", op)
                if op == "overview":
                    task = c.keyword_overview(keywords, options=options, **place)
                elif op == "volume":
                    task = c.google_ads_search_volume(keywords, options=options, **place)
                elif op == "difficulty":
                    refuse(op, options=options)
                    task = c.bulk_keyword_difficulty(keywords, **place)
                elif op == "trends":
                    refuse(op, options=options)
                    if len(keywords) > 5:
                        raise bad("op='trends' compares at most 5 keywords.")
                    task = c.google_trends_explore(keywords, date_from=date_from,
                                                   date_to=date_to, time_range=time_range,
                                                   **place)
                elif op == "ai_volume":
                    refuse(op, options=options)
                    task = c.ai_keyword_search_volume(keywords, **place)
                else:
                    refuse(op, options=options, location=location)
                    task = c.search_intent(keywords, language_code=place.get("language_code"),
                                           language_name=place.get("language_name"))
            elif op == "ideas":
                refuse(op, keyword=keyword, depth=depth)
                task = c.keyword_ideas(need(keywords, "keywords", op), options=options,
                                       **lists, **place)
            elif op == "suggestions":
                refuse(op, keywords=keywords, depth=depth)
                task = c.keyword_suggestions(need(keyword, "keyword", op), options=options,
                                             **lists, **place)
            elif op == "related":
                refuse(op, keywords=keywords)
                task = c.related_keywords(need(keyword, "keyword", op), depth=depth,
                                          options=options, **lists, **place)
            else:
                raise bad("op must be one of: overview, volume, difficulty, intent, "
                          "ideas, suggestions, related, trends, ai_volume.")
        bill(task)
        return view(task, full, flat=op == "volume")

    # --- domains ------------------------------------------------------------

    @mcp.tool()
    def dataforseo_domain(
        op: str,
        target: Optional[str] = None,
        target2: Optional[str] = None,
        targets: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        location: Optional[Union[int, str]] = None,
        language: Optional[str] = None,
        shared_only: Optional[bool] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
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

        Ops (≈$0.01 each unless said):
        - `overview` — ranking keyword count per position bucket, estimated
          monthly traffic (`etv`) and its paid value.
        - `history` — the same, month by month (`date_from`/`date_to`).
          `location` + `language` required. ≈$0.13.
        - `keywords` — the keywords the domain or page ranks for, with position,
          URL and volume (sorted as `order_by`, e.g. `["keyword_data.keyword_info.search_volume,desc"]`).
        - `pages` — its pages with their keyword counts and estimated traffic.
        - `subdomains` — its subdomains with keywords and estimated traffic.
        - `competitors` — domains ranking for the same keywords. `location` +
          `language` required.
        - `serp_competitors` — `keywords` (≤200): the domains that rank for THAT set
          of keywords, with average position and visibility. `location` +
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
            target / target2 / targets / keywords: per op.
            location / language: as in `dataforseo_keywords`; no location = all.
            shared_only: `gap` only.
            date_from / date_to: `history` only (`yyyy-mm-dd`).
            limit: rows (default 20, max 1000) — each row is billed.
            offset, filters, order_by, options, full: as in `dataforseo_keywords`.
        """
        place = loc(location, language)
        check_place("domain", op, place)
        if op != "history":
            refuse(op, date_from=date_from, date_to=date_to)
        if op != "serp_competitors":
            refuse(op, keywords=keywords)
        c, is_platform = _client()
        lists = dict(limit=limit_of(limit), offset=offset, filters=filters, order_by=order_by)
        with upstream(is_platform):
            if op == "traffic":
                refuse(op, target=target, target2=target2, shared_only=shared_only,
                       limit=limit, offset=offset, filters=filters, order_by=order_by)
                task = c.bulk_traffic_estimation(need(targets, "targets", op),
                                                 options=options, **place)
            elif op == "serp_competitors":
                refuse(op, target=target, target2=target2, targets=targets,
                       shared_only=shared_only)
                task = c.serp_competitors(need(keywords, "keywords", op), options=options,
                                          **lists, **place)
            elif op in ("overview", "history"):
                refuse(op, target2=target2, targets=targets, shared_only=shared_only,
                       limit=limit, offset=offset, filters=filters, order_by=order_by)
                t = need(target, "target", op)
                if op == "overview":
                    task = c.domain_rank_overview(t, options=options, **place)
                else:
                    refuse(op, options=options)
                    task = c.historical_rank_overview(t, date_from=date_from,
                                                      date_to=date_to, **place)
            elif op == "gap":
                refuse(op, targets=targets)
                task = c.domain_intersection(
                    need(target, "target", op), need(target2, "target2", op),
                    intersections=True if shared_only is None else shared_only,
                    options=options, **lists, **place)
            else:
                refuse(op, target2=target2, targets=targets, shared_only=shared_only)
                t = need(target, "target", op)
                if op == "keywords":
                    task = c.ranked_keywords(t, options=options, **lists, **place)
                elif op == "pages":
                    task = c.relevant_pages(t, options=options, **lists, **place)
                elif op == "subdomains":
                    task = c.subdomains(t, options=options, **lists, **place)
                elif op == "competitors":
                    task = c.competitors_domain(t, options=options, **lists, **place)
                elif op == "relevant_keywords":
                    task = c.keywords_for_site(t, options=options, **lists, **place)
                else:
                    raise bad("op must be one of: overview, history, keywords, pages, "
                              "subdomains, competitors, serp_competitors, gap, "
                              "relevant_keywords, traffic.")
        bill(task)
        return view(task, full)

    # --- backlinks ----------------------------------------------------------

    @mcp.tool()
    def dataforseo_backlinks(
        op: str,
        target: Optional[str] = None,
        targets: Optional[list[str]] = None,
        exclude_targets: Optional[list[str]] = None,
        mode: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        group_range: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Backlink profile of a domain, subdomain or page (DataForSEO index).

        `target`: a domain or subdomain WITHOUT `https://` or `www.`, or a page's
        absolute URL. Every op costs ≈$0.02 plus a little per row.

        Ops:
        - `summary` — totals: backlinks, referring domains, rank, spam score.
        - `list` — the backlinks (source URL, anchor, dofollow, rank, first/last
          seen); `mode`: `as_is` (default) | `one_per_domain` | `one_per_anchor`.
        - `referring_domains` — the domains linking to the target.
        - `anchors` — anchor texts pointing to the target.
        - `top_pages` — the target's most linked pages.
        - `competitors` — domains sharing the most backlinks with the target.
        - `gap` — `targets` (≤20, e.g. your competitors) and `exclude_targets`
          (≤10, e.g. your site): the domains linking to ALL targets and to none of
          the excluded — link-building prospects.
        - `history` — the profile month by month (since 2019; `date_from`).
        - `new_lost` — new and lost backlinks and referring domains per
          `group_range` (`day`|`week`|`month`|`year`, default month) since
          `date_from` (required); `date_to` defaults to yesterday.
        - `ranks` — `targets` (≤1000): the backlink rank of each, in one call.

        Returns — `{cost_usd, …, items: [...]}`, trimmed (`full=True` for the raw
        result, including the TLD / country / link-type breakdowns of `summary`).

        Args:
            op: see above.
            target / targets / exclude_targets: per op.
            mode: `list` only. group_range: `new_lost` only.
            date_from / date_to: `history` and `new_lost` (`yyyy-mm-dd`).
            limit: rows (default 20, max 1000) — each row is billed.
            offset, filters, order_by, full: as in `dataforseo_keywords`.
            options: other documented fields, e.g. `{"include_subdomains": false}`,
                `{"backlinks_status_type": "live"}`.
        """
        if op != "list":
            refuse(op, mode=mode)
        if op not in ("history", "new_lost"):
            refuse(op, date_from=date_from, date_to=date_to)
        if op != "new_lost":
            refuse(op, group_range=group_range)
        if op != "gap":
            refuse(op, exclude_targets=exclude_targets)
        if op in ("ranks", "gap"):
            refuse(op, target=target)
        else:
            refuse(op, targets=targets)
        c, is_platform = _client()
        with upstream(is_platform):
            if op in ("ranks", "summary", "history", "new_lost"):
                refuse(op, limit=limit, offset=offset, filters=filters, order_by=order_by)
                if op == "ranks":
                    task = c.bulk_ranks(need(targets, "targets", op), options=options)
                elif op == "summary":
                    task = c.backlinks_summary(need(target, "target", op), options=options)
                elif op == "history":
                    refuse(op, options=options)
                    task = c.backlinks_history(need(target, "target", op),
                                               date_from=date_from, date_to=date_to)
                else:
                    task = c.backlinks_new_lost(
                        need(target, "target", op), date_from=need(date_from, "date_from", op),
                        date_to=date_to or yesterday(), group_range=group_range,
                        options=options)
            else:
                lists = dict(limit=limit_of(limit), offset=offset, filters=filters,
                             order_by=order_by, options=options)
                if op == "gap":
                    task = c.backlinks_intersection(need(targets, "targets", op),
                                                    exclude_targets=exclude_targets, **lists)
                else:
                    t = need(target, "target", op)
                    if op == "list":
                        task = c.backlinks_list(t, mode=mode, **lists)
                    elif op == "referring_domains":
                        task = c.referring_domains(t, **lists)
                    elif op == "anchors":
                        task = c.anchors(t, **lists)
                    elif op == "top_pages":
                        task = c.backlinks_domain_pages(t, **lists)
                    elif op == "competitors":
                        task = c.backlinks_competitors(t, **lists)
                    else:
                        raise bad("op must be one of: summary, list, referring_domains, "
                                  "anchors, top_pages, competitors, gap, history, "
                                  "new_lost, ranks.")
        bill(task)
        return view(task, full)

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
        with upstream(is_platform):
            if op == "balance":
                refuse(op, query=query)
                if is_platform:
                    raise bad("op='balance' reads YOUR DataForSEO key; this call runs "
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
            raise bad("op must be `balance` or `locations`.")

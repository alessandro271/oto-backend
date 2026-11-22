"""LinkedIn Ads tool — the Advertising API, read-only, for audits.

Credential = the PERSON's LinkedIn sign-in (3-legged OAuth, scopes `r_ads` +
`r_ads_reporting`, never `rw_ads`), acquired and renewed by `auth/linkedin_ads.py`.
Wraps the lib's `oto.tools.linkedin_ads.LinkedInAdsClient`, one op per client method.

The connector is read-only, so it serves ONE tool, `linkedin_ads` (LECTURE), the read
verb in `op` — no `linkedin_ads_act` (`tests/test_convention_act.py`). Read-only holds
at three levels: the scopes of the token, a client that only issues GETs, and this
tool that has no op that writes.

**Not over-querying.** The LinkedIn application is the PLATFORM's: its daily call
limits and adAnalytics' 45 million metric values per 5 minutes are shared by every
person of every org. So each person has a guard of their own (`_Garde`), in the
process: calls per minute, analytics calls per minute, and the metric values their
analytics returned over 5 minutes — a refusal that says how long to wait, never a
sleep that would outlast the call, and never an automatic retry of a 429. Reports are
bounded by shape too: a start date required, at most 3 pivots and 18 metrics, at most
20 ids per facet, daily rows over at most 92 days, no "all pages" loop.

Every argument is checked against what `op` reads (`_ARGS`): one it does not read is
REFUSED, never silently dropped, and a missing required one is named. A 401 drops the
cached access token and retries ONCE with a renewed one.

Client calls are written out (`_client().search_campaigns(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError
from ..texte_tiers import cloture
from .lecture import LECTURE

if TYPE_CHECKING:  # the annotation only — the core is imported at call time
    from oto.tools.linkedin_ads import LinkedInAdsClient

_NAME = "linkedin_ads"

_PAGE = ("page_size", "page_token")

#: op → the arguments it reads, REQUIRED first.
_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "account_roles": ((), ()),
    "list_accounts": ((), ("statuses", "test", *_PAGE)),
    "get_account": (("account_id",), ()),
    "list_campaign_groups": (("account_id",), ("statuses", "test", *_PAGE)),
    "get_campaign_group": (("account_id", "campaign_group_id"), ()),
    "list_campaigns": (("account_id",), ("statuses", "campaign_group_ids",
                                         "campaign_types", "test", *_PAGE)),
    "get_campaign": (("account_id", "campaign_id"), ()),
    "list_creatives": (("account_id",), ("statuses", "campaign_ids", *_PAGE)),
    "get_creative": (("account_id", "creative_id"), ()),
    "analytics": (("pivots", "start"), ("end", "time_granularity", "account_ids",
                                        "campaign_group_ids", "campaign_ids",
                                        "fields")),
}
_OPS = tuple(_ARGS)

#: Campaign fields the default view keeps — what an audit reads; `full=True` returns
#: the campaign as LinkedIn sends it.
_CAMPAIGN_KEEP = ("id", "name", "status", "servingStatuses", "type", "format",
                  "objectiveType", "costType", "optimizationTargetType", "unitCost",
                  "dailyBudget", "totalBudget", "pacingStrategy", "runSchedule",
                  "campaignGroup", "audienceExpansionEnabled", "offsiteDeliveryEnabled",
                  "locale", "test")
_SLIM_OPS = ("list_campaigns", "get_campaign")
#: Values shown per targeting facet in the default view (the count is always given).
_FACETTE_MAX = 10
#: Daily rows beyond this span are refused: MONTHLY says the same in 30x fewer rows.
_JOURS_QUOTIDIENS_MAX = 92


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


# --- the per-person guard ---------------------------------------------------------

class _Garde:
    """Rolling windows per person, in the process. A refusal names the wait."""

    #: (window seconds, max events) per kind of call.
    FENETRES = {"call": (60.0, 40), "analytics": (60.0, 10)}
    #: Metric values one person's analytics may return over 5 minutes: a tenth of the
    #: application-wide 45 million, which every org shares.
    VALEURS_FENETRE_S = 300.0
    VALEURS_MAX = 4_500_000

    def __init__(self) -> None:
        self._verrou = threading.Lock()
        self._appels: dict[tuple[str, str], deque] = {}
        self._valeurs: dict[str, deque] = {}

    def entrer(self, qui: str, analytics: bool) -> None:
        now = time.monotonic()
        with self._verrou:
            kinds = ("call", "analytics") if analytics else ("call",)
            for kind in kinds:
                fenetre, plafond = self.FENETRES[kind]
                d = self._appels.setdefault((qui, kind), deque())
                while d and d[0] <= now - fenetre:
                    d.popleft()
                if len(d) >= plafond:
                    attente = int(d[0] + fenetre - now) + 1
                    quoi = "analytics reports" if kind == "analytics" else "LinkedIn Ads calls"
                    raise _bad(
                        f"Guard: at most {plafond} {quoi} per minute per person — the "
                        f"LinkedIn application's quota is shared. Wait {attente} s, and "
                        "prefer one wider report (more ids, a coarser granularity) over "
                        "many small ones. Nothing was sent.")
            if analytics:
                v = self._valeurs.setdefault(qui, deque())
                while v and v[0][0] <= now - self.VALEURS_FENETRE_S:
                    v.popleft()
                if sum(n for _, n in v) >= self.VALEURS_MAX:
                    attente = int(v[0][0] + self.VALEURS_FENETRE_S - now) + 1
                    raise _bad(
                        "Guard: your reports returned too many metric values in the last "
                        f"5 minutes (LinkedIn throttles the whole application beyond "
                        f"45 million). Wait {attente} s; ask fewer metrics, fewer pivots or "
                        "a coarser time_granularity. Nothing was sent.")
            for kind in kinds:
                self._appels[(qui, kind)].append(now)

    def compter(self, qui: str, lignes: int, metriques: int) -> None:
        with self._verrou:
            self._valeurs.setdefault(qui, deque()).append(
                (time.monotonic(), max(lignes, 0) * max(metriques, 1)))


_GARDE = _Garde()


# --- checks and shaping ------------------------------------------------------------

def _check(op: str, given: dict) -> None:
    """No argument is silently dropped: one that `op` does not read is refused, a
    required one that is missing is named."""
    required, optional = _ARGS[op]
    stray = sorted(k for k, v in given.items() if v is not None
                   and k not in required and k not in optional)
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")
    missing = [k for k in required if given.get(k) in (None, "", [])]
    if missing:
        raise _bad(f"op='{op}' requires {', '.join(f'`{k}`' for k in missing)}"
                   + (" (from op='list_accounts' or op='account_roles')"
                      if "account_id" in missing else "")
                   + ". Nothing was done.")


def _facettes(noeud: Any, out: dict) -> None:
    """Collects `{facet URN: [values]}` from a `targetingCriteria` include/exclude
    tree (`and` / `or` nesting), whatever its depth."""
    if isinstance(noeud, dict):
        for k, v in noeud.items():
            if k in ("and", "or", "include", "exclude"):
                _facettes(v, out)
            elif isinstance(v, list) and k.startswith("urn:li:"):
                out.setdefault(k, []).extend(v)
            else:
                _facettes(v, out)
    elif isinstance(noeud, list):
        for item in noeud:
            _facettes(item, out)


def _ciblage(criteria: Any) -> dict:
    """`{include: {facet: {count, values}}, exclude: {…}}` — facet names shortened
    (`urn:li:adTargetingFacet:locations` → `locations`), values capped."""
    resume: dict = {}
    if not isinstance(criteria, dict):
        return resume
    for cote in ("include", "exclude"):
        trouve: dict = {}
        _facettes(criteria.get(cote), trouve)
        if trouve:
            resume[cote] = {
                facette.rsplit(":", 1)[-1]: {"count": len(vals),
                                             "values": vals[:_FACETTE_MAX]}
                for facette, vals in trouve.items()}
    return resume


def _campagne(camp: Any) -> Any:
    # Not `c`: the version-skew probe reads a name `c` as the client.
    if not isinstance(camp, dict):
        return camp
    out = {k: camp[k] for k in _CAMPAIGN_KEEP if k in camp}
    if "targetingCriteria" in camp:
        out["targeting_summary"] = _ciblage(camp["targetingCriteria"])
    return out


def _shape(op: str, payload: Any, full: bool) -> Any:
    if full or op not in _SLIM_OPS or not isinstance(payload, dict):
        return payload
    if op == "get_campaign":
        return _campagne(payload)
    return {**payload, "elements": [_campagne(x) for x in payload.get("elements") or []]}


def _jour(nom: str, valeur: Optional[str]) -> Optional[_dt.date]:
    if valeur is None:
        return None
    try:
        return _dt.date.fromisoformat(str(valeur).strip())
    except ValueError:
        raise _bad(f"`{nom}` must be a date YYYY-MM-DD; got {valeur!r}. "
                   "Nothing was done.") from None


def _upstream_message(e) -> str:
    """A LinkedIn refusal (4xx but 401), by status, its body fenced."""
    status = e.status_code
    if status == 400:
        texte = ("LinkedIn rejected the request as invalid (400): check the ids, the "
                 "statuses, the pivots and the metric names.")
    elif status == 403:
        texte = ("LinkedIn refused the read (403): the person signed in has no role on "
                 "this ad account (Campaign Manager → account → Manage access; VIEWER is "
                 "enough), or the application lacks the Advertising API.")
    elif status == 404:
        texte = "LinkedIn: not found (404) — check the account id and the object id."
    elif status == 429:
        attente = f" (LinkedIn asks to wait {int(e.retry_after)} s)" if e.retry_after else ""
        texte = (f"LinkedIn rate limit reached (429){attente}. Do not retry at once: the "
                 "application's daily limits reset at midnight UTC and are shared — "
                 "wait, then send fewer, wider requests.")
    else:
        texte = f"LinkedIn refused the request (HTTP {status})."
    if e.body in (None, "", {}):
        return texte
    return texte + cloture("upstream-error-body", str(e.body),
                           origine="Refusal written by LinkedIn",
                           lecture="a diagnostic")


# --- the client and the call -------------------------------------------------------

def _client() -> LinkedInAdsClient:
    """The LinkedIn Ads client of THIS caller. The name and the UNQUOTED annotation
    are a contract of the version-skew probe."""
    from ..auth import linkedin_ads as li_auth

    jeton = li_auth.access_token_for(access.current_user_sub_or_raise())
    return li_auth._paquet().LinkedInAdsClient(jeton)


def _run(fn, *, analytics: bool = False):
    from oto.tools.common.errors import UpstreamHTTPError

    from ..auth import linkedin_ads as li_auth

    sub = access.current_user_sub_or_raise()
    _GARDE.entrer(sub, analytics)
    for tentative in (1, 2):
        try:
            return fn()
        except UpstreamHTTPError as e:
            if e.status_code == 401:
                li_auth.oublier_jeton(sub)
                if tentative == 1:
                    continue
                message = ("LinkedIn rejects the authorization (HTTP 401) even with a "
                           "renewed token: reconnect from your connectors, « LinkedIn "
                           "Ads ».")
                li_auth.marquer_rejet(sub, message)
                raise _bad(message)
            if 400 <= e.status_code < 500:
                raise _bad(_upstream_message(e))
            raise
        except RuntimeError as e:
            # Dead or expired grant, application refused or not configured, core missing.
            raise _bad(str(e))
        except ValueError as e:
            # The client's argument check (ids, dates, closed choices, bounds).
            raise _bad(f"{e} Nothing was done.")


def _analytics(pivots, start, end, time_granularity, account_ids, campaign_group_ids,
               campaign_ids, fields) -> Any:
    debut, fin = _jour("start", start), _jour("end", end)
    granularite = time_granularity or "ALL"
    if granularite == "DAILY" and debut is not None:
        jours = ((fin or _dt.datetime.now(_dt.timezone.utc).date()) - debut).days + 1
        if jours > _JOURS_QUOTIDIENS_MAX:
            raise _bad(f"time_granularity='DAILY' over {jours} days is refused (at most "
                       f"{_JOURS_QUOTIDIENS_MAX}): use MONTHLY, or narrow the range. "
                       "Nothing was done.")
    res = _run(lambda: _client().get_analytics(
        pivots, start, end, time_granularity=granularite, account_ids=account_ids,
        campaign_group_ids=campaign_group_ids, campaign_ids=campaign_ids,
        fields=fields), analytics=True)
    lignes = (res or {}).get("elements") or [] if isinstance(res, dict) else []
    _GARDE.compter(access.current_user_sub_or_raise(), len(lignes),
                   len(fields or ()) or 8)
    if isinstance(res, dict) and not lignes:
        res = {**res, "hint": (
            "No rows. LinkedIn answers EMPTY — not a refusal — both when there was no "
            "activity in the range and when the person signed in has no read access "
            "to these accounts or campaigns. Check op='account_roles', then the dates.")}
    return res


def register(mcp: FastMCP) -> None:
    # NO import from the core here: the connector stays mounted (and refuses, saying
    # so) while the pinned oto-core tag does not ship `oto.tools.linkedin_ads`.
    from ..auth import linkedin_ads as li_auth

    li_auth.avertir_au_demarrage()

    @mcp.tool(name="linkedin_ads", annotations=LECTURE)
    def linkedin_ads(
        op: Literal[_OPS] = "account_roles",
        account_id: Optional[str] = None,
        campaign_group_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        creative_id: Optional[str] = None,
        statuses: Optional[list[str]] = None,
        campaign_group_ids: Optional[list[str]] = None,
        campaign_ids: Optional[list[str]] = None,
        campaign_types: Optional[list[str]] = None,
        test: Optional[bool] = None,
        pivots: Optional[list[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        time_granularity: Optional[Literal["ALL", "DAILY", "MONTHLY", "YEARLY"]] = None,
        account_ids: Optional[list[str]] = None,
        fields: Optional[list[str]] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """LinkedIn Ads (Campaign Manager), read-only, with the person's own LinkedIn
        sign-in: ad accounts, campaign groups, campaigns with their budget, bid and
        targeting, ads, and performance reports. Nothing is created, edited, paused or
        spent — the token itself cannot write.

        Start with `op="account_roles"`: the ad accounts the person holds a role on
        (VIEWER is enough), with that role. Their numeric id is the `account_id` the
        other ops need. An account the person has no role on is invisible: ask its owner
        to add them in Campaign Manager (account → Manage access).

        `op`:
        - `account_roles`, `list_accounts` (status, currency, serving statuses),
          `get_account`.
        - `list_campaign_groups`, `get_campaign_group` — group budget and schedule.
        - `list_campaigns`, `get_campaign` — objective, format, cost type and bid
          (`unitCost`), `dailyBudget` / `totalBudget`, schedule, serving statuses, and
          a `targeting_summary` (facets with their values count); `full=True` for the
          raw campaign with its `targetingCriteria`.
        - `list_creatives`, `get_creative` — status, `isServing`, `servingHoldReasons`,
          review status, the sponsored content's URN.
        - `analytics` — adAnalytics: `pivots` (1 to 3: CAMPAIGN, CREATIVE,
          CAMPAIGN_GROUP, ACCOUNT, CONVERSION, PLACEMENT_NAME, …; a MEMBER_* audience
          pivot — MEMBER_JOB_FUNCTION, MEMBER_SENIORITY, MEMBER_COMPANY_SIZE,
          MEMBER_INDUSTRY, MEMBER_COUNTRY_V2, MEMBER_COMPANY — only alone), `start`
          required, `end` (default today), `time_granularity` ALL | MONTHLY | DAILY
          (≤ 92 days) | YEARLY, and at least one of `account_ids`,
          `campaign_group_ids`, `campaign_ids` (≤ 20 each; per-ad rows are
          `pivots=["CREATIVE"]` over them). `fields`:
          up to 18 metrics, default impressions, clicks, costInLocalCurrency,
          landingPageClicks, totalEngagements, externalWebsiteConversions,
          oneClickLeads, videoViews. Rows carry `pivotValues` (URNs) and `dateRange`.

        Lists return `{elements, next_page_token}`: pass `page_token` for the next page,
        one page per call. Amounts are in the account currency (`costInLocalCurrency`);
        audience pivots are approximate, top 100 values per creative per day, delayed
        24-48 h. An empty report is NOT proof of no activity: it is also what LinkedIn
        returns without access.

        Calls are paced per person (the LinkedIn application's quota is shared): a
        refusal says how long to wait. Prefer one report with several ids and a coarse
        granularity over a loop of small ones.

        Not here: writes of any kind, revenue attribution, names of targeting URNs,
        lead forms, audiences.

        Args:
            op: the read, see above.
            account_id: every op but account_roles / list_accounts / analytics —
                digits, from op='account_roles'.
            campaign_group_id: get_campaign_group — digits or its URN.
            campaign_id: get_campaign — digits or its URN.
            creative_id: get_creative — digits or urn:li:sponsoredCreative:<digits>.
            statuses: list_accounts (ACTIVE, CANCELED, DRAFT, …), list_campaign_groups /
                list_campaigns (ACTIVE, PAUSED, ARCHIVED, COMPLETED, DRAFT, CANCELED, …;
                default: all but deleted), list_creatives (intended statuses).
            campaign_group_ids: list_campaigns filter, or an analytics facet.
            campaign_ids: list_creatives filter, or an analytics facet.
            campaign_types: list_campaigns — TEXT_AD | SPONSORED_UPDATES |
                SPONSORED_INMAILS | DYNAMIC.
            test: lists — true = test entities only, false = real ones only.
            pivots: analytics — 1 to 3 pivots, see above.
            start: analytics — first day, YYYY-MM-DD (UTC, inclusive).
            end: analytics — last day, YYYY-MM-DD (inclusive); default today.
            time_granularity: analytics — ALL (default), DAILY, MONTHLY, YEARLY.
            account_ids: analytics facet — ad account ids.
            fields: analytics — metric names (camelCase, ≤ 18).
            page_size: lists — up to 1000 (100 for creatives).
            page_token: lists — `next_page_token` of the previous page.
            full: list_campaigns / get_campaign — True returns the campaign as LinkedIn
                sends it (whole `targetingCriteria`, audit stamps).
        """
        _check(op, {
            "account_id": account_id, "campaign_group_id": campaign_group_id,
            "campaign_id": campaign_id, "creative_id": creative_id,
            "statuses": statuses, "campaign_group_ids": campaign_group_ids,
            "campaign_ids": campaign_ids, "campaign_types": campaign_types,
            "test": test, "pivots": pivots, "start": start, "end": end,
            "time_granularity": time_granularity, "account_ids": account_ids,
            "fields": fields,
            "page_size": page_size, "page_token": page_token,
        })
        if full and op not in _SLIM_OPS:
            raise _bad(f"op='{op}' does not take `full`: its answer already comes whole. "
                       "Nothing was done.")
        if op == "account_roles":
            return _run(lambda: _client().list_account_roles())
        if op == "list_accounts":
            return _run(lambda: _client().search_ad_accounts(
                statuses=statuses, test=test, page_size=page_size, page_token=page_token))
        if op == "get_account":
            return _run(lambda: _client().get_ad_account(account_id))
        if op == "list_campaign_groups":
            return _run(lambda: _client().search_campaign_groups(
                account_id, statuses=statuses, test=test, page_size=page_size,
                page_token=page_token))
        if op == "get_campaign_group":
            return _run(lambda: _client().get_campaign_group(account_id,
                                                             campaign_group_id))
        if op == "list_campaigns":
            return _shape(op, _run(lambda: _client().search_campaigns(
                account_id, statuses=statuses, campaign_group_ids=campaign_group_ids,
                types=campaign_types, test=test, page_size=page_size,
                page_token=page_token)), full)
        if op == "get_campaign":
            return _shape(op, _run(lambda: _client().get_campaign(account_id,
                                                                  campaign_id)), full)
        if op == "list_creatives":
            return _run(lambda: _client().search_creatives(
                account_id, campaign_ids=campaign_ids, intended_statuses=statuses,
                page_size=page_size, page_token=page_token))
        if op == "get_creative":
            return _run(lambda: _client().get_creative(account_id, creative_id))
        return _analytics(pivots, start, end, time_granularity, account_ids,
                          campaign_group_ids, campaign_ids, fields)

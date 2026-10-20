"""The tech-stack cascade (`waterfall_tech`): what a company's website runs, then what
its job postings name — and, with `technologies`, a source counts only when it detects
one of them."""
from __future__ import annotations

import re
from typing import Optional

from .etape import Step
from .fournisseurs import _APIFY_TIMEOUT_S, _apify_items
from .valeurs import _get, _norm_name, _same_company

# Two kinds of source, measured on one domain (20/10/2026): what the WEBSITE runs (CMS,
# front framework, hosting, analytics — DataForSEO's index, a live crawl) — 6-7 names;
# and what the JOB POSTINGS name (CRM, ERP, data stack, internal tools — TheirStack) —
# ~100 slugs, but dearer and thin on small companies. Cheapest first, as everywhere.


def _tech_key(name) -> str:
    """`Next.js`, `next-js`, `nextjs` → one key: the website sources give display names,
    TheirStack gives slugs (`google-cloud-platform`, `spendesk1`)."""
    return re.sub(r"\d+$", "", _norm_name(str(name or "")).replace(" ", ""))


def _tech_find(names: list, inputs: dict, source: str, by_category=None) -> Optional[dict]:
    """The find: the names, deduplicated. With `technologies` asked, a source counts
    only when it detects at least one of them — a stack without them is not an answer
    to « does it use X? », and the next source (which sees other things) is tried."""
    names = list(dict.fromkeys(str(n) for n in names if n))
    if not names:
        return None
    found = {"technologies": names, "count": len(names), "source": source}
    if by_category:
        found["by_category"] = by_category
    wanted = inputs.get("wanted")
    if wanted:
        have = {_tech_key(n) for n in names}
        matched = [w for w in wanted if _tech_key(w) in have]
        if not matched:
            return None
        found["matched"] = matched
    return found


def _dataforseo_tech(res, inputs):
    """`technologies` is `{group: {category: [names]}}` — flattened by category."""
    groups = _get(res, "technologies")
    if not isinstance(groups, dict):
        return None
    by_category: dict[str, list] = {}
    for cats in groups.values():
        for cat, names in (cats or {}).items() if isinstance(cats, dict) else ():
            by_category.setdefault(cat, []).extend(n for n in names or () if n)
    return _tech_find([n for ns in by_category.values() for n in ns], inputs, "website",
                      by_category)


def _website_tech(res, inputs):
    for item in _apify_items(res)[:1]:
        if not isinstance(item, dict) or item.get("error"):
            continue
        names = [t.get("name") for t in item.get("technologies") or ()
                 if isinstance(t, dict)]
        return _tech_find(names, inputs, "website", item.get("byCategory") or None)
    return None


def _theirstack_tech(res, inputs):
    for rec in (_get(res, "data") or [])[:3]:
        if isinstance(rec, dict) and _same_company(inputs, rec.get("name"),
                                                   rec.get("domain")):
            return _tech_find(rec.get("technology_names") or [], inputs, "job_postings")
    return None


def _theirstack_tech_args(inputs: dict) -> dict:
    # Billed PER COMPANY returned: the domain names one; a name may need a namesake
    # stepped over (3).
    if inputs.get("domain"):
        return {"extra": {"company_domain_or": [inputs["domain"]]}, "limit": 1}
    return {"extra": {"company_name_case_insensitive_or": [inputs["company_name"]]},
            "limit": 3}


TECH_STEPS: tuple[Step, ...] = (
    Step("dataforseo", "dataforseo_technologies", (("domain",),),
         lambda i: {"op": "domain", "target": i["domain"]},
         _dataforseo_tech, "~$0.01-0.02 per domain (DataForSEO)",
         label="Website (DataForSEO index)"),
    # The live crawl — plain HTTP, no browser, billed per site analysed (a site that
    # fails or times out is free): a site DataForSEO has not indexed (small companies),
    # or a stack changed since its last visit.
    Step("website", "apify_run_sync", (("domain",),),
         lambda i: {"actor_id": "kfirs/tech-stack-detector",
                    "run_input": {"urls": [i["domain"]], "minConfidence": 50,
                                  "includeDetails": False, "timeoutSecs": 20},
                    # ⚠️ No `max_items`: pay-per-event, Apify would derive a ceiling
                    # under its $0.50 minimum (see the LinkedIn page). One URL = one
                    # billed site at most.
                    "timeout_secs": _APIFY_TIMEOUT_S},
         _website_tech, "~$0.01 of Apify per site", label="Website (live crawl)"),
    Step("theirstack", "theirstack_companies_search", (("domain",), ("company_name",)),
         _theirstack_tech_args, _theirstack_tech, "~3 TheirStack credits per company",
         label="Job postings (TheirStack)", max_units=3),
)

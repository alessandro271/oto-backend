"""A call's inputs, normalized — the same builders for a one-contact call and for a
table's row (`tools/waterfall_lignes.py`): their checks (a 9-digit SIREN, a LinkedIn
company page) refuse the caller's own faults."""
from __future__ import annotations

import re

from .valeurs import _bad, _company_page, _domain, _email

def _person_inputs(first_name, last_name, full_name, domain, company_name,
                   linkedin_url, email) -> dict:
    first_name = (first_name or "").strip() or None
    last_name = (last_name or "").strip() or None
    full_name = (full_name or "").strip() or None
    if full_name and not (first_name and last_name):
        parts = full_name.split()
        if len(parts) >= 2:
            first_name = first_name or parts[0]
            last_name = last_name or " ".join(parts[1:])
    if not full_name and first_name and last_name:
        full_name = f"{first_name} {last_name}"
    return {
        "first_name": first_name, "last_name": last_name, "full_name": full_name,
        "domain": _domain(domain) or _domain(email),
        "company_name": (company_name or "").strip() or None,
        "linkedin_url": (linkedin_url or "").strip() or None,
        "email": _email(email),
    }


def _company_inputs(company_name, domain, siren, country) -> dict:
    siren_digits = re.sub(r"\D", "", siren or "") or None
    if siren and (not siren_digits or len(siren_digits) != 9):
        raise _bad(f"`siren` must have 9 digits — got `{siren}`.")
    c = (country or "").strip().upper() or None
    return {"company_name": (company_name or "").strip() or None,
            "domain": _domain(domain), "siren": siren_digits,
            "fr": c in (None, "FR", "FRA", "FRANCE")}


#: The most people one call returns — a bound on the RESPONSE'S size (an agent reads it),
#: not on spend: the call's `limit` (10 by default) is the caller's to raise.
PEOPLE_MAX = 100


def _people_inputs(company_name, domain, company_linkedin_url, roles, country,
                   limit) -> dict:
    if isinstance(roles, str):
        roles = [r for r in re.split(r"[,;]", roles)]
    roles = [r.strip() for r in (roles or []) if r and r.strip()]
    page = _company_page(company_linkedin_url) if company_linkedin_url else None
    if company_linkedin_url and not page:
        raise _bad(f"`company_linkedin_url` must be a LinkedIn company page "
                   f"(linkedin.com/company/…) — got `{company_linkedin_url}`.")
    n = max(1, min(int(limit or 10), PEOPLE_MAX))
    return {"company_name": (company_name or "").strip() or None,
            "domain": _domain(domain), "company_linkedin_url": page,
            "roles": roles or None, "country": (country or "").strip() or None,
            "limit": n}


def _tech_inputs(company_name, domain, technologies) -> dict:
    if isinstance(technologies, str):
        technologies = re.split(r"[,;]", technologies)
    wanted = [t.strip() for t in (technologies or []) if t and t.strip()]
    return {"company_name": (company_name or "").strip() or None,
            "domain": _domain(domain), "wanted": wanted or None}

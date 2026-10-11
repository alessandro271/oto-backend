"""Registry declaration of the `dataforseo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# dataforseo: SEO data — keyword volumes and ideas, a domain's rankings, traffic
# and competitors, backlinks. HTTP Basic auth on two fields (API login + API
# password), hence `secret_kind="fields"`. Pay-as-you-go: no seat, no plan, a
# prepaid USD balance drawn per call — which is what makes a platform key
# resellable, unlike Ahrefs (expensive named seat, byo-only). Every call reports
# its real USD cost, traced as `quantity` in micro-dollars (`tools/dataforseo.py`),
# so billing is the real spend times a rate.
# Paid platform key, like `forager`: posted CLOSED (`set_platform_key`), it serves
# only the orgs granted to it — the host enrols and cuts orgs on its own billing.
# ⚠️ /admin posts a platform key as ONE value: it is `login:password`, split by
# `tools/dataforseo.py::_creds` (a JSON blob of both fields works too).
CONNECTOR = _c(
    "dataforseo", ["dataforseo"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False, secret_kind="fields",
    label="DataForSEO",
    help="SEO data — keyword volume and ideas, a domain's rankings, traffic and "
         "competitors, backlinks (pay-as-you-go)",
    publisher="DataForSEO", href="https://app.dataforseo.com/api-access",
    credential_fields=(
        CredentialField("login", "API login (your DataForSEO email)", secret=False),
        CredentialField("password", "API password", secret=True,
                        help="Dashboard → API Access. Not your sign-in password."),
    ),
)

CATEGORY = "Marketing"
PUBLISHER = "DataForSEO"
LOGO_DOMAIN = "dataforseo.com"

DESCRIPTION = (
    "SEO data from DataForSEO: Google keyword volume, CPC, difficulty, intent and "
    "ideas; a domain's ranking keywords, estimated traffic, top pages and "
    "competitors, and the keyword gap between two sites; backlinks, referring "
    "domains and anchors. Pay-as-you-go, billed per call on its real cost. "
    "Runs on your own DataForSEO API login, or on the shared key when it is "
    "granted to your org."
)

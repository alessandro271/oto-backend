"""Registry declaration of the `linkedin_ads` connector — the LinkedIn ad accounts a
person can read, through LinkedIn's Advertising API. READ-ONLY.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# Same situation as `meta_ads` and `jefacture`: the instance's LinkedIn application
# (Developer Portal, Advertising API product approved by LinkedIn), its `client_id` at
# PLATFORM scope of `connector_settings` and its `client_secret` in the vault
# (`auth/app_instance`), the flow hosted by oto
# (`auth/linkedin_ads.py`). ON BEHALF OF THE PERSON: each one signs in with their own
# LinkedIn account, and the token reads the ad accounts on which THEY hold a Campaign
# Manager role — an agency auditing a prospect's account asks the prospect for a
# VIEWER role first.
#
# Read-only BY ITS SCOPES (`r_ads`, `r_ads_reporting`, never `rw_ads`): even a bug
# in a tool cannot change an ad account. Not the LinkedIn connectors already served
# (`linkedin_unipile`: the member's session; `aiark`, `apify`: profile data) — none of
# them reads Campaign Manager.
CONNECTOR = _c(
    "linkedin_ads", ["linkedin_ads"],
    auth_modes={"byo_user"},
    # Consent comes from the person's LinkedIn account, not from their org.
    personal_session=True, secret_kind="oauth",
    label="LinkedIn Ads",
    help="The LinkedIn ad accounts you have a role on (Campaign Manager): campaign "
         "groups, campaigns with their budgets and targeting, ads, and their "
         "performance. You sign in with LinkedIn; read-only.",
    href="https://www.linkedin.com/campaignmanager",
)

CATEGORY = "Marketing & SEO"
# Who receives the call: LinkedIn — its official Marketing API, its consent page.
PUBLISHER = "LinkedIn"
LOGO_DOMAIN = "linkedin.com"

DESCRIPTION = (
    "LinkedIn advertising through LinkedIn's official Advertising API: the ad accounts "
    "you have a Campaign Manager role on, their campaign groups, campaigns (budget, "
    "bid, objective, targeting) and ads (status, review, serving holds), and "
    "performance reports (impressions, clicks, spend, conversions, leads) by campaign, "
    "ad, day or audience (job function, seniority, company size, country). Read-only — "
    "nothing is created, edited, paused or spent."
)

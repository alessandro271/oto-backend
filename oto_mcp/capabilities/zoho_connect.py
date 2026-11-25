"""Zoho: the start of a consent (shared by the generic connection flow), and the
Analytics organizations of a connected account.

`start_for` builds the consent URL for `me.connector_connect` (fixed path
`POST /api/me/connectors/{name}/connect`, through the flow declared in `tools/zoho.py`):
the dashboard's Connect button, which asks the region. An agent no longer gets this URL —
it carries a single-use `state` —, it gets the card's page from `oto_connector_connect`
(`capabilities/connectors/connect_link.py`), where the person clicks.

What REMAINS a hand-written route (`api/oauth_returns.py`): the **callback**.
Zoho redirects the BROWSER there — with no auth header, with a 302 response — which
a capability contract (JSON + authz) cannot express. This is declared as
such in `test_rest_modules_are_capabilities.py`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access
from ..auth import zoho as zoho_oauth
from ..connectors import flow as connector_flow
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


def start_for(ctx: ResolvedCtx, connector: str, data_center: str,
              return_app: str = "") -> connector_flow.FlowStart:
    """Consent URL to open. The app (client_id/secret) comes from the VAULT — never
    from an env variable: the app dedicated by the team or the org wins, then the one
    brought as a credential, otherwise we take the region's PUBLISHER app
    (`credentials_store` §publisher app), which gives everyone the "one click". The
    scopes are those of the access level in force for the org and the active team;
    org, team and level are signed in the state, and the callback replays the same
    resolution.

    Shared with the generic flow (`connector_flow`, declared in tools/zoho.py): there
    must be only ONE way to start a Zoho consent, otherwise the two
    surfaces diverge — exactly what the convergence seeks to avoid."""
    # The TARGET connector (zoho, zohodesk, zohoanalytics) — the MCP face is named
    # under `zoho`, so the call guard alone does not see which one is asked.
    from ..connectors.activation_gate import exiger_connectable_capacite
    exiger_connectable_capacite(connector, ctx.sub)
    try:
        org = ctx.org_id or 0
        group = access.current_group(ctx.sub)
        app = zoho_oauth.app_fields(connector, ctx.sub, data_center or "",
                                    org=org, group=group)
        url = zoho_oauth.build_auth_url(
            ctx.sub, org, connector, zoho_oauth.consent_region(app, data_center),
            app=app, return_app=return_app, group_id=group,
            level=zoho_oauth.consent_level(connector, org, group))
    except zoho_oauth.ZohoRegionMissing as e:
        raise AuthzDenied(400, "zoho_region_missing", str(e))
    except zoho_oauth.ZohoNoApp as e:
        raise AuthzDenied(400, "zoho_no_app", str(e))
    except zoho_oauth.ZohoOAuthError as e:
        raise AuthzDenied(400, "zoho_oauth_unavailable", str(e))
    # The connector echo is SPECIFIC to Zoho (three connectors share this flow):
    # it goes down into `details`, the top level remaining common to all flows.
    return connector_flow.FlowStart(auth_url=url, details={"connector": connector})


class AnalyticsOrgsInput(BaseModel):
    pass


def _analytics_orgs(ctx: ResolvedCtx, inp: AnalyticsOrgsInput) -> dict:  # noqa: ARG001
    """The Analytics organizations of the connected account, so the user can CHOOSE.

    Zoho Analytics requires an organization on every call, and an account often sees
    several (shared workspaces). Without this list, all that was left was to
    ask for an eleven-digit identifier — which nobody knows by heart and which
    has to be looked up in the Zoho interface. Here we return NAMES.

    When only one organization exists, it was already set at consent
    (`zoho_oauth._derived_fields`): this surface therefore only serves the ambiguous case,
    and confirms it (`current` = the one that is saved)."""
    try:
        fields = access.resolve_credential(
            "zohoanalytics", want="byo", sub=ctx.sub, emit_on_failure=False).fields or {}
    # noqa: SILENT — declared debt: vault error read as "no credential" (#424, verdict C)
    except Exception:  # noqa: BLE001
        fields = {}
    if not fields.get("refresh_token"):
        raise AuthzDenied(400, "zoho_analytics_not_connected",
                          "connect Zoho Analytics first — the list of "
                          "organizations comes from your account.")
    try:
        orgs = zoho_oauth.analytics_orgs(fields)
    except Exception as e:  # noqa: BLE001
        raise AuthzDenied(502, "zoho_analytics_orgs_failed", str(e))
    return {"orgs": orgs, "current": fields.get("org_id") or None}


# `platform.instructions` pattern (ADR 0042): ONE op-aware capability for MCP +
# per-verb capabilities for REST, same handlers. The REST faces are
# idiomatic (one path = one verb) and MCP keeps a consolidated surface
# (ADR 0047, one tool per business object).
class AnalyticsOrgs(BaseModel):
    """The Zoho Analytics organizations visible to the current credential, and the one
    that is pinned. `current` is None until someone has chosen — this is what
    triggers the question to the user rather than an arbitrary choice."""
    orgs: list[dict]
    current: Optional[object] = None


CAPABILITIES += [
    Capability(
        key="me.zoho_analytics_orgs",
        handler=_analytics_orgs,
        Input=AnalyticsOrgsInput,
        Output=AnalyticsOrgs,
        authz=ORG_MEMBER,
        mcp="zohoanalytics_orgs",
        rest=RestBinding("GET", "/api/me/connectors/zohoanalytics/orgs"),
        description=(
            "Zoho Analytics organizations visible to your account (id, name, role) + "
            "the one currently saved. Analytics requires an organization on "
            "every call and an account often sees several (shared "
            "workspaces): this is where you choose it, from names rather than an "
            "identifier."),
    ),
]

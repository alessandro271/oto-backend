"""The start of a Salesforce consent — shared by the generic connection flow.

`start_for` builds the consent URL for `me.connector_connect` (fixed path
`POST /api/me/connectors/{name}/connect`, through the flow declared in
`tools/salesforce.py`): the dashboard's Connect button. An agent no longer gets this URL —
it carries a single-use `state` —, it gets the card's page from `oto_connector_connect`
(`capabilities/connectors/connect_link.py`), where the person clicks.

The callback stays a hand-written route (`api/oauth_returns.py`): Salesforce redirects the
BROWSER there, with no auth header, which a capability contract cannot express.

Salesforce particularity (cf. `salesforce_oauth.py`): the OAuth client is
**per-customer** (each org creates its own Connected App), so `start` reads the
client_id/client_secret/login_url triplet ALREADY set on the connector card — it is a
prerequisite, not a platform constant.
"""
from __future__ import annotations

from typing import Optional

from ..auth import salesforce as salesforce_oauth
from ..connectors import flow as connector_flow
from ._types import AuthzDenied, ResolvedCtx


def start_for(ctx: ResolvedCtx, scope: str,
              return_app: Optional[str] = None) -> connector_flow.FlowStart:
    """Consent URL to open, for the requested level (`member`, `org`, `group`). The right
    is checked when the link is built AND re-checked on return (the state lives 10 min —
    cf. the callback). `return_app`: the front end whose browser asked."""
    try:
        auth_url = salesforce_oauth.build_auth_url(ctx.sub, scope or "member", return_app)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_scope_param", str(e))
    except PermissionError as e:
        raise AuthzDenied(403, "org_admin_required", str(e))
    except LookupError as e:
        raise AuthzDenied(400, "missing_credentials", str(e))
    except RuntimeError as e:
        raise AuthzDenied(400, "oauth_misconfigured", str(e))
    # The RETAINED tier is specific to Salesforce (other flows have none): it
    # goes down into `details`. It carries information — it is the effective scope, default
    # resolved — but no generic client needs to know it in order to open the URL.
    return connector_flow.FlowStart(auth_url=auth_url, details={"scope": scope or "member"})

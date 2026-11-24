"""Capability "complete a connector's connection" — ONE path, every OAuth return.

The sibling of `POST /api/me/connectors/{name}/connect` (`connect.py`). The provider
brings the browser back to the connector's callback, which stores nothing: it sends the
front to `?connector=<card>&connect=confirm`, with the `code` and the `state` in the URL
fragment. The front posts them here, with the token of the person signed in. The
connection completes for the person who started it (the state's `sub` must be the
bearer's), once (the state is consumed), then the connector exchanges and stores.

The mechanics live in `auth/flow.py` (`complete`); each provider declares its return
from its module (`oauth_flow.declare_return`). The card travels as a path parameter,
like for `connect`, and must be the one the state carries.

`POST /api/google/oauth/complete` (`capabilities/federated_oauth.py`) is the same
completion for a Google card, under its own path and its own response shape.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ...auth import flow as oauth_flow
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES


class ConnectorCompleteInput(BaseModel):
    """`code` and `state` as the callback handed them to the front, in the fragment of
    the return URL (`#code=…&state=…`)."""
    name: str                                   # the card, from the path
    code: str
    state: str


class ConnectorConnectionCompleted(BaseModel):
    """The connection is stored. `connector` = the card that asked for it, `account` = the
    account stored when the provider names it (an address, a workspace, a site), `scope` =
    who it is entrusted to (`member`, or `org` / `group` for a shared connection).

    `warning` and `details` are present only when the connector has something to say
    about a connection that succeeded (Zoho: the consented organization could not be
    checked, `details.consented_org` names it)."""
    ok: bool
    connector: str
    account: Optional[str] = None
    scope: str
    warning: Optional[str] = None
    details: Optional[dict] = Field(
        default=None,
        description="Connector-specific information about the stored connection.")


def complete_for(retour: oauth_flow.OAuthReturn, sub: Optional[str], code: str,
                 state: str, *, card: Optional[str] = None) -> dict:
    """`oauth_flow.complete`, its named refusals translated into the capability's."""
    try:
        return oauth_flow.complete(retour, sub, code, state, card=card)
    except oauth_flow.CompletionRefused as e:
        raise AuthzDenied(e.status, e.code, e.message, details=e.details) from None


def _complete(ctx: ResolvedCtx, inp: ConnectorCompleteInput) -> dict:
    retour = oauth_flow.return_for_card(inp.name)
    if retour is None:
        raise AuthzDenied(404, "unknown_connector",
                          f"\"{inp.name}\" has no connection to complete.")
    return complete_for(retour, ctx.sub, inp.code, inp.state, card=inp.name)


#: The refusals shared by every completion, Google's path included.
COMPLETION_ERRORS = (
    DeclaredError(400, "invalid_state",
                  "state unreadable, altered, expired, from another flow or another card"),
    DeclaredError(403, "state_not_yours", "state issued for another person than the bearer"),
    DeclaredError(409, "state_used", "state already used"),
    DeclaredError(502, "exchange_failed",
                  "the provider refused the exchange of the code, or storing failed"),
)

_ERREURS = COMPLETION_ERRORS + (
    DeclaredError(404, "unknown_connector", "this card has no connection to complete"),
    DeclaredError(403, "not_allowed",
                  "the person no longer has the right to write at the level asked at start"),
    DeclaredError(403, "admin_required",
                  "the person's organization requires an administrator's approval"),
    DeclaredError(409, "org_mismatch",
                  "the consented organization is not the expected one; "
                  "`details.consented_org` names it"),
)

CAPABILITIES += [
    Capability(
        key="me.connector_complete",
        handler=_complete,
        Input=ConnectorCompleteInput,
        authz=SUB_ONLY,
        Output=ConnectorConnectionCompleted,
        mcp=None,     # a browser return: the front holds the code, never an agent
        errors=_ERREURS,
        rest=RestBinding(verb="POST", path="/api/me/connectors/{name}/complete"),
        description=("Completes a connection: the front posts the `code` and the `state` "
                     "the callback handed it in the fragment (`connect=confirm`). The "
                     "state must have been issued for the BEARER (`state_not_yours`) and "
                     "for this card (`invalid_state`), it is used once (`state_used`), "
                     "then the connector exchanges the code and stores the connection."),
    ),
]

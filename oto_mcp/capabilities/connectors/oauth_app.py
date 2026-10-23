"""Capabilities "dedicated OAuth app" — the app an org or a team sets for its consents.

The app (`client_id`/`client_secret`, and for Zoho its region) is INFRASTRUCTURE, the
token is an IDENTITY (`docs/connector-vault.md` §Application ≠ token). Set as a BYO
credential at org level, the app was resolved by the call cascade like a key — a bare
app, without a `refresh_token`. Here it is stored APART (`credentials_store` §dedicated
app, `PLATFORM` / `app:<owner>:<id>`), never served by the cascade, read by the consent
alone, in this order: team's dedicated app > org's dedicated app > BYO credential >
publisher app (`zoho_oauth.app_fields`).

- **REST only**: a raw secret never travels as a tool argument (repo rule).
- **org** (`/api/orgs/{id}/connectors/{name}/oauth-app`): `ORG_ADMIN_OF`; **team**
  (`/api/groups/{id}/connectors/{name}/oauth-app`): `GROUP_ADMIN_OF`.
- **The GET never returns a secret**, nor even the `client_id`: `configured`, the
  non-secret config (`data_center`), when and by whom it was set, and a 4-character
  fingerprint per secret field (`journal_secrets.fingerprint`, bound to the vault row) —
  the `me.credential` contract (`docs/rest-api.md`, #671). To change it, set it again.
- **Closed list** (`_READERS`): only the connectors whose consent READS a dedicated app,
  each with the config check of its flow. Any other is refused, 400
  `oauth_app_unsupported`: set there, the app would serve nobody.
"""
from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel

from ... import credentials_store, journal_secrets, org_store, providers
from ...auth import zoho as zoho_oauth
from .._authz import GROUP_ADMIN_OF, ORG_ADMIN_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_GID = {"id": "group_id"}
_SECRETS = ("client_id", "client_secret")


def _zoho_config(inp) -> dict:
    """A Zoho app is registered in ONE data center: the region is part of the app. The
    Zoho organization it is meant for, optional, is checked at each consent."""
    try:
        config = {"data_center": zoho_oauth.normalize_region(inp.data_center)}
    except zoho_oauth.ZohoOAuthError as e:
        raise AuthzDenied(400, "invalid_oauth_app", str(e))
    org = (inp.zoho_org_id or "").strip()
    return {**config, "zoho_org_id": org} if org else config


# The connectors whose consent flow reads a dedicated app → the config check of their
# flow. A connector enters here with its reader (`zoho_oauth.app_fields`), never before.
_READERS: dict[str, Callable[..., dict]] = {
    c: _zoho_config for c in zoho_oauth.CONNECTORS}


class OrgOAuthAppInput(BaseModel):
    org_id: int
    name: str


class OrgOAuthAppSetInput(OrgOAuthAppInput):
    client_id: str
    client_secret: str
    data_center: Optional[str] = None
    zoho_org_id: Optional[str] = None


class GroupOAuthAppInput(BaseModel):
    group_id: int
    name: str


class GroupOAuthAppSetInput(GroupOAuthAppInput):
    client_id: str
    client_secret: str
    data_center: Optional[str] = None
    zoho_org_id: Optional[str] = None


class OAuthAppState(BaseModel):
    """The dedicated app of a scope — never a secret. `configured: false` = none set:
    the consent reads the next origin. `read_fingerprints` = `{secret field: 4
    characters}`, an HMAC bound to the vault row, never characters of the secret.
    `deleted` (clear only): `false` = there was nothing to remove (idempotent)."""
    connector: str
    scope: str
    org_id: int
    group_id: Optional[int] = None
    configured: bool
    data_center: Optional[str] = None
    zoho_org_id: Optional[str] = None
    read_set_at: Optional[str] = None
    read_set_by: Optional[str] = None
    read_fingerprints: dict[str, str] = {}
    deleted: Optional[bool] = None


def _connector(name: str) -> str:
    if name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connector `{name}` unknown.")
    if name not in _READERS:
        raise AuthzDenied(400, "oauth_app_unsupported",
                          f"`{name}` does not read a dedicated OAuth app: set here, it "
                          f"would serve nobody. Connectors that read one: "
                          f"{', '.join(sorted(_READERS))}.")
    return name


def _exiger_org(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} unknown.")


def _state(scope: str, owner_id: int, name: str, org_id: int,
           group_id: Optional[int]) -> dict:
    out = {"connector": name, "scope": scope, "org_id": org_id, "group_id": group_id,
           "configured": False, "read_fingerprints": {}}
    app = credentials_store.get_dedicated_app(scope, owner_id, name)
    if app is None:
        return out
    label = credentials_store.dedicated_app_label(scope, owner_id)
    fields, set_at = app["fields"], app.get("set_at")
    return {**out, "configured": True, "data_center": fields.get("data_center"),
            "zoho_org_id": fields.get("zoho_org_id"),
            "read_set_at": set_at if isinstance(set_at, (str, type(None)))
            else set_at.isoformat(),
            "read_set_by": app.get("set_by"),
            "read_fingerprints": {
                f: journal_secrets.fingerprint(credentials_store.PLATFORM, label, name,
                                               "", f, fields[f])
                for f in _SECRETS if fields.get(f)}}


def _set(scope: str, owner_id: int, name: str, inp, sub: str) -> None:
    fields = {"client_id": inp.client_id.strip(),
              "client_secret": inp.client_secret.strip(),
              **_READERS[name](inp)}
    if not (fields["client_id"] and fields["client_secret"]):
        raise AuthzDenied(400, "invalid_oauth_app", "client_id and client_secret required.")
    credentials_store.set_dedicated_app(scope, owner_id, name, fields, set_by=sub)


def _org_get(ctx: ResolvedCtx, inp: OrgOAuthAppInput) -> dict:
    name = _connector(inp.name)
    _exiger_org(inp.org_id)
    return _state("org", inp.org_id, name, inp.org_id, None)


def _org_set(ctx: ResolvedCtx, inp: OrgOAuthAppSetInput) -> dict:
    name = _connector(inp.name)
    _exiger_org(inp.org_id)
    _set("org", inp.org_id, name, inp, ctx.sub)
    return _state("org", inp.org_id, name, inp.org_id, None)


def _org_clear(ctx: ResolvedCtx, inp: OrgOAuthAppInput) -> dict:
    name = _connector(inp.name)
    _exiger_org(inp.org_id)
    deleted = credentials_store.clear_dedicated_app("org", inp.org_id, name)
    return {**_state("org", inp.org_id, name, inp.org_id, None), "deleted": deleted}


def _group_get(ctx: ResolvedCtx, inp: GroupOAuthAppInput) -> dict:
    name = _connector(inp.name)
    return _state("group", inp.group_id, name, ctx.org_id, inp.group_id)


def _group_set(ctx: ResolvedCtx, inp: GroupOAuthAppSetInput) -> dict:
    name = _connector(inp.name)
    _set("group", inp.group_id, name, inp, ctx.sub)
    return _state("group", inp.group_id, name, ctx.org_id, inp.group_id)


def _group_clear(ctx: ResolvedCtx, inp: GroupOAuthAppInput) -> dict:
    name = _connector(inp.name)
    deleted = credentials_store.clear_dedicated_app("group", inp.group_id, name)
    return {**_state("group", inp.group_id, name, ctx.org_id, inp.group_id),
            "deleted": deleted}


_ORG = "/api/orgs/{id}/connectors/{name}/oauth-app"
_GROUP = "/api/groups/{id}/connectors/{name}/oauth-app"
_ERREURS = (DeclaredError(404, "unknown_connector", "connecteur absent du registre"),
            DeclaredError(400, "oauth_app_unsupported",
                          "le flux du connecteur ne lit pas d'app dédiée"))
_ERREURS_POSE = _ERREURS + (
    DeclaredError(400, "invalid_oauth_app", "app incomplète, ou région inconnue"),)
_ORG_ERR = (DeclaredError(404, "unknown_org", "l'org n'existe pas"),)
_DIT = ("Never returns a secret: `configured`, the region, when and by whom, and a "
        "4-character fingerprint per secret field.")

CAPABILITIES += [
    Capability(
        key="connectors.oauth_app.org_get", handler=_org_get,
        Input=OrgOAuthAppInput, Output=OAuthAppState,
        authz=ORG_ADMIN_OF("org_id"), errors=_ERREURS + _ORG_ERR,
        description="[org admin] The OAuth app your org dedicates to the consents of a "
                    "connector. " + _DIT,
        rest=RestBinding("GET", _ORG, _ID),
    ),
    Capability(
        key="connectors.oauth_app.set_org", handler=_org_set,
        Input=OrgOAuthAppSetInput, Output=OAuthAppState,
        authz=ORG_ADMIN_OF("org_id"), errors=_ERREURS_POSE + _ORG_ERR,
        description="[org admin] Set or rotate the OAuth app your org dedicates to the "
                    "consents of a connector (`client_id`, `client_secret`, and for Zoho "
                    "`data_center`, optionally `zoho_org_id`: the Zoho organization each "
                    "consent is checked against). Read by the consent only, never served "
                    "as a key.",
        rest=RestBinding("PUT", _ORG, _ID),
    ),
    Capability(
        key="connectors.oauth_app.clear_org", handler=_org_clear,
        Input=OrgOAuthAppInput, Output=OAuthAppState,
        authz=ORG_ADMIN_OF("org_id"), errors=_ERREURS + _ORG_ERR,
        description="[org admin] Remove your org's dedicated OAuth app: new consents "
                    "fall back on the brought credential, then on the publisher app.",
        rest=RestBinding("DELETE", _ORG, _ID),
    ),
    Capability(
        key="connectors.oauth_app.group_get", handler=_group_get,
        Input=GroupOAuthAppInput, Output=OAuthAppState,
        authz=GROUP_ADMIN_OF("group_id"), errors=_ERREURS,
        description="[team lead] The OAuth app your team dedicates to the consents of a "
                    "connector. " + _DIT,
        rest=RestBinding("GET", _GROUP, _GID),
    ),
    Capability(
        key="connectors.oauth_app.set_group", handler=_group_set,
        Input=GroupOAuthAppSetInput, Output=OAuthAppState,
        authz=GROUP_ADMIN_OF("group_id"), errors=_ERREURS_POSE,
        description="[team lead] Set or rotate the OAuth app your team dedicates to the "
                    "consents of a connector — it takes precedence over the org's.",
        rest=RestBinding("PUT", _GROUP, _GID),
    ),
    Capability(
        key="connectors.oauth_app.clear_group", handler=_group_clear,
        Input=GroupOAuthAppInput, Output=OAuthAppState,
        authz=GROUP_ADMIN_OF("group_id"), errors=_ERREURS,
        description="[team lead] Remove your team's dedicated OAuth app: the org's "
                    "applies.",
        rest=RestBinding("DELETE", _GROUP, _GID),
    ),
]

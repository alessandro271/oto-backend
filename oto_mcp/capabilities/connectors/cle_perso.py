"""Capabilities "personal key policy" — an org admin forbids personal keys, per connector.

Decision of 13/10/2026 (oto-backend#1239): « for <connector>, no personal key in this
org ». The machinery and what the policy means live in `connectors/cle_perso.py`; here,
the two verbs exposed to the org admin: read the policy, set or lift it.

It is not the per-member restriction removed on 24/09 (ADR 0053 D1): nobody is named.
Reserving a connector to a team = this policy + a key set on the team (or the included
key granted to it): members of the team resolve the team key, the others have nothing.

Read and write = `ORG_ADMIN_OF`. No `refresh_visibility`: the policy hides no tool, it
removes a key tier, read at every call.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import org_store, providers
from ...connectors import cle_perso
from .._authz import ORG_ADMIN_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}      # placeholder {id} → Input field org_id


class PersonalKeyPolicyListInput(BaseModel):
    org_id: int


class PersonalKeyPolicySetInput(BaseModel):
    org_id: int
    name: str                  # connector (placeholder {name}, auto-mapped)
    forbidden: bool


class PersonalKeyPolicyRow(BaseModel):
    """A connector whose personal keys the org forbids. `connector` is the CARRIER of
    the credential (the unipile channels share `unipile`'s key)."""
    connector: str
    set_by: Optional[str] = None
    set_at: Optional[str] = None


class PersonalKeyPolicies(BaseModel):
    """The org's policy. A connector absent from `forbidden` keeps its personal keys."""
    org_id: int
    forbidden: list[PersonalKeyPolicyRow]


class PersonalKeyPolicySet(BaseModel):
    """Echo of the gesture. `forbidden=false` = the policy is lifted (idempotent: it
    does not prove one existed)."""
    org_id: int
    connector: str
    forbidden: bool


def _exiger_org(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} unknown.")


def _iso(v) -> Optional[str]:
    return v if isinstance(v, (str, type(None))) else v.isoformat()


def _list(ctx: ResolvedCtx, inp: PersonalKeyPolicyListInput) -> dict:
    _exiger_org(inp.org_id)
    return {"org_id": inp.org_id,
            "forbidden": [{**r, "set_at": _iso(r["set_at"])}
                          for r in cle_perso.politiques(inp.org_id)]}


def _set(ctx: ResolvedCtx, inp: PersonalKeyPolicySetInput) -> dict:
    if inp.name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connector `{inp.name}` unknown.")
    _exiger_org(inp.org_id)
    cle_perso.poser(inp.org_id, inp.name, inp.forbidden, set_by=ctx.sub)
    return {"org_id": inp.org_id, "connector": cle_perso.porteur(inp.name),
            "forbidden": inp.forbidden}


_PATH = "/api/orgs/{id}/connectors"

CAPABILITIES += [
    Capability(
        key="connectors.personal_key_policy.org_list", handler=_list,
        Input=PersonalKeyPolicyListInput, Output=PersonalKeyPolicies,
        authz=ORG_ADMIN_OF("org_id"),
        errors=(DeclaredError(404, "unknown_org", "l'org n'existe pas"),),
        description="[org admin] List the connectors for which your org forbids personal "
                    "keys: only a team key, the org key or the included key serve them.",
        rest=RestBinding("GET", _PATH + "/personal-key-policy", _ID),
    ),
    Capability(
        key="connectors.personal_key_policy.set_org", handler=_set,
        Input=PersonalKeyPolicySetInput, Output=PersonalKeyPolicySet,
        authz=ORG_ADMIN_OF("org_id"),
        errors=(DeclaredError(404, "unknown_connector", "connecteur absent du registre"),
                DeclaredError(404, "unknown_org", "l'org n'existe pas")),
        description="[org admin] Forbid (`forbidden=true`) or allow again personal keys "
                    "for one connector in your org. Forbidden: calls skip every member's "
                    "personal key and are refused by name when no team, org or included "
                    "key serves; connecting or setting a personal key is refused. "
                    "Reserve a connector to a team = forbid personal keys + set the key "
                    "on that team.",
        rest=RestBinding("PUT", _PATH + "/{name}/personal-key-policy", _ID),
    ),
]

"""Capability `connector.connect` (`oto_connector_connect`) — the address where a PERSON
connects a connector, asked for by the agent before any call fails.

The same answer as a connection refusal (`connectors/connect_link`, contract
`refus-de-connexion.md` of the connectors lib), on demand: the agent learns the gesture
(`oauth`, `key`, `hosted_link`, `admin_activation`), who does it (`by`) and the page where
it is done (`url`), and hands that page to the person. It replaces the two per-connector
verbs `zoho_connect` and `salesforce_connect`, which returned a consent URL carrying a
single-use `state`: a secret relayed through the agent's context, and a third flow away
from a third verb.

**What it never returns**: a consent URL, a `state`, a code or any token. The page it
gives starts nothing when opened; its button does, under the person's own session —
`POST /api/me/connectors/{name}/connect` (capability `me.connector_connect`), then the
completion by the session that started it (oto-backend#1914). So a link relayed to the
wrong person links nothing: whoever opens it acts under their own account, in an org the
page re-reads from its path, with rights the server re-checks at every gesture.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import access, db, providers, roles
from ...connectors import activation_gate, connect_link
from ...connectors import readiness as connector_readiness
from .._authz import ORG_MEMBER
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx
from ..registry import CAPABILITIES
from . import selection as sel


class ConnectorConnectLinkInput(BaseModel):
    connector: str = Field(pattern=r"^[a-z][a-z0-9_]*$",
                           description="The connector's exact name (catalog `name`).")
    space: Optional[str] = Field(
        None, pattern=r"^(me|org|team:[^\s:]+)$",
        description="Where the account is connected: `me` (default — the person's own), "
                    "`org` (the org's shared account) or `team:<group_id>`.")
    access: Optional[str] = Field(
        None, description="Not used on this platform: no connector declares several "
                          "accesses. Omit it.")


class _Owner(BaseModel):
    kind: Literal["org", "team", "user"]
    description: Optional[str] = None
    name: Optional[str] = None
    yours: Optional[bool] = None


class _Account(BaseModel):
    id: str
    label: str
    mode: str
    owner: _Owner


class ConnectorConnectLink(BaseModel):
    """Either a gesture to do (`url`, `gesture`, `by`), or `gesture: "none"` with the
    `account` that already serves the connector here (no `url`)."""
    gesture: Literal["oauth", "key", "hosted_link", "admin_activation", "none"]
    connector: str
    space: str
    url: Optional[str] = Field(None, description="The page where the gesture is done. "
                               "Opening it starts nothing: the person clicks there.")
    by: Optional[Literal["user", "administrator"]] = None
    account: Optional[_Account] = None


def _refus_page(e: connect_link.PageDeConnexionAbsente) -> AuthzDenied:
    return AuthzDenied(409, "connect_page_undeclared", str(e))


def _equipe(ctx: ResolvedCtx, equipe: str) -> int:
    """The team of `team:<id>`, if the caller can see it — else the same refusal as a team
    that does not exist (no oracle on other teams)."""
    if not equipe.isdigit() or not roles.can_read_group(ctx.sub, int(equipe)):
        raise AuthzDenied(404, "unknown_team",
                          f"No team `{equipe}` you can see: `space` takes `team:<group_id>`.")
    return int(equipe)


def _owner(win, sub: str) -> dict:
    """Who the serving account belongs to — a NAME, never a person's identifier."""
    if win.mode == "user":
        titulaire = str(win.entity_id or "").partition(":")[2]
        yours = titulaire == sub
        nom = ((db.get_user(titulaire) or {}).get("name") or "").strip()
        if "@" in nom:          # a name that is an address: never served (contract)
            nom = ""
        return {"kind": "user", "name": nom or ("you" if yours else "a member of your org"),
                "yours": yours}
    if win.mode == "group":
        return {"kind": "team", "description": win.account or ""}
    if win.mode == "org":
        return {"kind": "org", "description": win.account or ""}
    # tenant / platform: a key the org does not hold (the contract has no owner kind for
    # it); said in the description, the mode tells it apart.
    return {"kind": "org", "description": f"{win.mode} key, provided to your org"}


def _compte_qui_sert(sub: str, connector: str, org, group) -> dict:
    """The account the call would use here (same walk as the call, presence only — no
    secret is read). Its id is the INSTANCE's (`inst:<id>`), a resource, never a person's
    identifier; a personal account without an instance row falls back to its ref only
    when it is the caller's own."""
    from ...access import cascade, chain_shadow
    from ... import instance_refs
    win = next(chain_shadow.resolution_rungs(sub, connector, org=org, group=group,
                                             probe=cascade.PRESENCE_PROBE, want="auto"),
               None)
    if win is None:
        raise RuntimeError(f"`{connector}` is ready but no account resolves for it")
    porteur = providers.credential_provider(connector)
    owner = _owner(win, sub)
    inst = (db.instance_id_for_vault_row(win.entity_type or "", win.entity_id or "",
                                         porteur, win.account or "")
            if win.entity_type else None)
    if inst is not None:
        ident = instance_refs.make_instance_ref(inst)
    elif win.mode != "user" or owner.get("yours"):
        ident = instance_refs.ref_for_credential(win.entity_type or "", win.entity_id or "",
                                                 porteur, win.account or "")
    else:
        ident = None
    if not ident:
        raise RuntimeError(f"the account serving `{connector}` has no designatable id")
    con = providers.REGISTRY.get(connector)
    return {"id": ident, "label": win.account or (con.label if con else connector),
            "mode": win.mode, "owner": owner}


def _connect_link(ctx: ResolvedCtx, inp: ConnectorConnectLinkInput) -> dict:
    sub, org, connector = ctx.sub, ctx.org_id, inp.connector
    if connector not in providers.REGISTRY:
        raise sel._refus_nom_inconnu(sel._visible_catalog(ctx), connector, ctx)
    if inp.access:
        raise AuthzDenied(400, "access_not_declared",
                          f"`{connector}` declares no accesses on this platform: omit `access`.")
    group = access.current_group(sub)

    # Cut in this org or team: an administrator reopens it, on the org's or the team's
    # connectors page. Cut by the tenant or the platform: nobody in the org can.
    coupure = activation_gate._coupure(connector, org, group)
    if coupure is not None:
        cran, pourquoi, geste = coupure
        if cran not in ("org", "group"):
            raise AuthzDenied(409, "connector_cut_upstream",
                              f"`{connector}` is {pourquoi}: no page of your org can "
                              f"reopen it. {geste[0].upper()}{geste[1:]}")
        space = "org" if cran == "org" else connect_link.espace_d_equipe(group)
        peut = (roles.is_org_admin(sub, org) if cran == "org"
                else roles.can_admin_group(sub, group))
        try:
            url = connect_link.adresse_du_geste(connector, "admin_activation", sub=sub,
                                                org=org, space=space)
        except connect_link.PageDeConnexionAbsente as e:
            raise _refus_page(e) from None
        return {"gesture": "admin_activation", "connector": connector, "space": space,
                "url": url, "by": "user" if peut else "administrator"}

    gesture = connect_link.geste(connector)
    if gesture is None:
        raise AuthzDenied(400, "no_account_needed",
                          f"`{connector}` needs no account: call its tools directly.")
    space = inp.space or "me"
    sorte, equipe = connect_link.espace(space)
    if sorte == "me":
        by = "user"
        # Already served here: say which account, no gesture to make.
        if connector_readiness.diagnose(sub, connector, org=org, group=group) is None:
            return {"gesture": "none", "connector": connector, "space": space,
                    "account": _compte_qui_sert(sub, connector, org, group)}
    elif sorte == "org":
        by = "user" if roles.is_org_admin(sub, org) else "administrator"
    else:
        by = "user" if roles.can_admin_group(sub, _equipe(ctx, equipe)) else "administrator"
    try:
        url = connect_link.adresse_du_geste(connector, gesture, sub=sub, org=org,
                                            space=space)
    except connect_link.PageDeConnexionAbsente as e:
        raise _refus_page(e) from None
    return {"gesture": gesture, "connector": connector, "space": space, "url": url,
            "by": by}


CAPABILITIES += [
    Capability(
        key="connector.connect",
        handler=_connect_link,
        Input=ConnectorConnectLinkInput,
        Output=ConnectorConnectLink,
        authz=ORG_MEMBER,
        # Cross-cutting (`oto_`): it serves EVERY connector, including one not in the
        # caller's toolbox yet — that is when it is needed.
        mcp="oto_connector_connect",
        rest=None,
        errors=(
            DeclaredError(404, "unknown_connector", "the name designates no connector"),
            DeclaredError(400, "access_not_declared", "`access` is passed: no connector "
                          "declares several accesses here"),
            DeclaredError(409, "connector_cut_upstream", "the connector is cut by the "
                          "tenant or the platform: no page of the org reopens it"),
            DeclaredError(400, "no_account_needed", "the connector has no account"),
            DeclaredError(404, "unknown_team", "`space: team:<id>` names a team the "
                          "caller cannot see"),
            DeclaredError(409, "connect_page_undeclared", "this instance or tenant "
                          "declares no page for this gesture (OTO_DASHBOARD_LINK_PATHS)"),
        ),
        description=(
            "The page where a PERSON connects a connector — call it when a tool needs an "
            "account the person has not connected, or after a refusal whose `data.oto` "
            "carries `gesture`/`url` (the same answer). Returns `gesture` (`oauth`: they "
            "click Connect and approve at the provider; `key`: they paste the key in the "
            "form; `hosted_link`: they log in to their account in the hosted window; "
            "`admin_activation`: an administrator re-enables the connector), `by` "
            "(`user`: the person does it; `administrator`: send the link to an admin of "
            "the org or team) and `url`. Send `url` to the person: opening it starts "
            "nothing, they act on that page under their own account. Never ask for a key, "
            "password or token in the conversation. `gesture: \"none\"` = an account "
            "already serves this connector here (`account`): just call it. When they say "
            "it is done, call again or retry the tool. `space`: `me` (default), `org`, "
            "`team:<group_id>`."),
    ),
]

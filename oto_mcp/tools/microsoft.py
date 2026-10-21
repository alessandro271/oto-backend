"""The Microsoft 365 account — the carrier that each Microsoft service (SharePoint &
OneDrive, Outlook, Outlook Calendar, Teams, OneNote) borrows.

A single, read-only tool, the twin of `google_accounts`: `microsoft_accounts` — the
linked accounts and, for each, the services it has AUTHORISED and the directory it
signs in to. It is the question each service raises: `sharepoint_file` on an account
that has not authorised SharePoint is refused, naming the card to open. No network
call: what the vault knows (`meta.scopes`, refreshed at every token renewal).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access
from ..auth import microsoft as ms_auth


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def microsoft_accounts() -> dict:
        """List the linked Microsoft 365 accounts and the services each one has authorised.

        Returns {accounts: [{account, email, is_default, services, directory}]} where
        `services` names the Microsoft services this account consented to (sharepoint…).
        A service missing from the list has not been authorised on that account yet:
        connect it from its own connector card. `directory` is the client's directory the
        account signs in to (a guest account), null for its home directory.
        `shared` (present only then) = "group" or "org": an account an admin connected on
        behalf of your team or your whole organization, `shared_owner` ({id, name}) that
        team or org — not yours to revoke.
        `lent_by` (present only then) = the e-mail (or name) of the colleague who LENT you
        that account: not yours to revoke; `lent_via_group` ({id, name}) when it is lent to
        a team of yours rather than to you, `lent_via_org` ({id, name}) when it is lent to
        your whole organization.
        Pass `account` as `_account` to the service tools; omitted, the project's
        pinned account, else your only one, else your default one, else your team's
        account, else your organization's (several at one of these levels and no default:
        refused, naming them). A lent account is used ONLY when `_account` names it: with
        no account of your own, your team's or your organization's, a call without
        `_account` is refused and lists the lent accounts.
        """
        sub = access.current_user_sub_or_raise()
        comptes = ms_auth.avec_noms(ms_auth.accounts_for(sub))
        preteurs = access.libelles_preteurs(c["lent_by"] for c in comptes
                                            if c.get("lent_by"))
        return {
            "accounts": [
                {"account": c["account"],
                 "email": (c.get("meta") or {}).get("email"),
                 "is_default": bool((c.get("meta") or {}).get("is_default")),
                 "services": ms_auth.services_granted((c.get("meta") or {}).get("scopes")),
                 "directory": (c.get("meta") or {}).get("tenant"),
                 **({"shared": c["shared"], "shared_owner": c["shared_owner"]}
                    if c.get("shared") else {}),
                 # Lent by a peer (`oto_instance op=lend`): used only when `_account`
                 # names it, not yours to revoke.
                 **({"lent_by": preteurs[c["lent_by"]]} if c.get("lent_by") else {}),
                 **({"lent_via_group": c["via_group"]} if c.get("via_group") else {}),
                 **({"lent_via_org": c["via_org"]} if c.get("via_org") else {})}
                for c in comptes
            ]
        }

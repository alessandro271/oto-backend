"""Registry declaration of the `entra` connector — the Microsoft 365 directory (Microsoft
Entra ID), READ, on the Microsoft 365 account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# entra: the organization's directory via Microsoft Graph, ON BEHALF OF THE PERSON —
# scope `DIRECTORY` (users and licences, groups and members, sign-ins, sites), READ only.
# Its scopes need a tenant administrator's approval, never included in the default
# « approve oto for the whole organization » (`auth/microsoft.ADMIN_ONLY_SERVICES`); the
# person's own Microsoft role still bounds what Graph returns.
CONNECTOR = service(
    "entra",
    label="Microsoft 365 directory",
    help="your organization's Microsoft 365 directory, read only: accounts, licences, "
         "groups and their members, sites — for its administrators",
    href="https://learn.microsoft.com/graph/api/resources/users",
)

CATEGORY = "Business apps"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Your organization's Microsoft 365 directory, read only, for its administrators: "
    "every account with its licences and last sign-in, the subscriptions bought and "
    "used, the groups, teams and mailing lists with their members, the SharePoint "
    "sites. Nothing is changed."
)

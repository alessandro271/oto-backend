## prerequisite — an administrator approves, then connects

the directory is read and changed **with the rights of the person connected**, and its permissions (read the whole directory and the sign-in logs, assign licences, enable accounts, manage group members) always need a Microsoft 365 administrator.
- an administrator opens **Approve for my organization** on the "Microsoft 365 directory" card (`microsoft_admin_consent(services=["entra"])`): this approval is never part of the approval of the other Microsoft services
- then she clicks **Authorize Microsoft 365 directory** on the card, with her work account
- Microsoft still bounds every call by her role: a Global Reader or a User Administrator sees the accounts and licences; the last sign-in needs a reporting role **and** an Entra ID P1 licence on the organization (included in Business Premium and the E plans, not in Business Basic or Standard); a licence is changed by a License or User Administrator, an account enabled by a User Administrator, a group's members by a Groups Administrator or the group's owner

## usage — the state of the directory

`entra` reads, under the account the call names (`_account=`), otherwise the default one.
- "which licences do we pay, how many are used" → `entra(op="licenses")`
- "who still has a licence but has left" → `entra(op="users", licensed=true, sign_in=true)`, then compare with the HR list of current staff; without P1, drop `sign_in`
- "the disabled accounts that still hold a licence" → `entra(op="users", enabled=false, licensed=true)`
- "who is on Business Standard" → `entra(op="users", license="Business Standard")`
- "the mailing lists and their members" → `entra(op="groups", kind="distribution")`, then `entra(op="group", group_id=…)`
- "everything about Jane" → `entra(op="user", user="jane@…")`: licences, manager, groups

## usage — changing it

`entra_act` acts at once, for the person concerned: confirm with the user before each change.
- "give Jane a Business Standard licence" → `entra_act(op="add_license", user="jane@…", license="Business Standard")`
- "Paul has left" → `entra_act(op="disable_account", user="paul@…")`, then `entra_act(op="remove_license", …)` for each of his licences
- "add Jane to the Quality team" → `entra(op="groups", query="quality")` for its id, then `entra_act(op="add_member", user="jane@…", group_id=…)`: she gets the team and its SharePoint site

## note — what is misleading

- creating or deleting an account and resetting a password are done in the Microsoft 365 admin centre, never here
- a licence needs the user's **usage location** (`usageLocation` in `op="user"`), set in the admin centre; without it Microsoft refuses the licence
- removing a licence cuts its services (mailbox, OneDrive, Teams) once Microsoft's grace period ends
- the members of a mailing list or a mail-enabled security group are changed in Exchange: `add_member` refuses them
- the members of a team site are those of its Microsoft 365 group (`op="group"`); access granted inside SharePoint itself (SharePoint groups, sharing links) is **not** listed
- `op="sites"` lists the sites the connected person can see, not necessarily all of them
- `op="user"` lists the groups an account is a **direct** member of
- a licence that Microsoft names by a code (`skuPartNumber`) not known to oto is shown as that code

## note — scope

accounts, licences and subscriptions, last sign-in, groups (Microsoft 365, security, mailing lists) and their members and owners, team sites, visible SharePoint sites; written: licences, enabled accounts, members of security and Microsoft 365 groups. A person's mail, files or calendar are the other Microsoft cards.

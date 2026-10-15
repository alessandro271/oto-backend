"""The LENT account rule — common to every connector, applied by Microsoft first.

Measured in production: a borrower with no Microsoft account of her own was served,
without knowing it, the account a colleague had lent her (search, draft, deletion in the
colleague's mailbox); the moment she linked her own, the same conversation switched to
it, with no signal at all. Two rules close it, written once here:

1. **A lent account serves only when the call NAMES it** (`_account=`): never the only
   loan by default. Without a name and without a candidate that is not lent, a refusal
   `account_required` lists the loans and who lent them (`pret_a_servir`).
   ⚠️ "Lent" = lent by ANOTHER PERSON, to the caller or to a team of hers (ADR 0044
   `share_side`). The accounts that belong to the org or the team THEMSELVES are not
   loans: they keep being served without being named, like the caller's own.
2. **Every response says which account served and, for a loan, whose it is**: the
   descriptor `rappel_du_compte` (`_account`, `lent_by`, `lent_via_group`), rendered by
   `middleware/call_context._echo_account`.

A candidate is a dict `{account, lent_by?, via_group?, …}`: `lent_by` = the sub of the
lender (absent = not a loan), `via_group` = `{id, name}` of the borrowing team (absent =
lent to the caller by name). The connector keeps whatever else it needs on it.

Wired today: Microsoft (`auth/microsoft._lent_for_call`) and the echo (every connector:
the descriptor is read from the row that served). The other connectors' resolutions are
the next batch.
"""
from __future__ import annotations

from typing import Iterable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, db
from ..mcp_errors import McpError


def libelles_preteurs(subs: Iterable[str]) -> dict[str, str]:
    """`{sub: label}` — how the person who LENT an account is named to whoever uses it:
    their e-mail (what the borrower recognises), else their name. Never the bare sub,
    which tells a borrower nothing; a lender with no profile row at all (nothing else
    exists to name them) is named by the sub. One query for the e-mails, one per lender
    without one."""
    wanted = sorted({str(s) for s in subs if s})
    emails = db.emails_by_subs(wanted) if wanted else {}
    out = {}
    for s in wanted:
        if emails.get(s):
            out[s] = emails[s]
            continue
        u = db.get_user(s) or {}
        out[s] = u.get("name") or s
    return out


def etiquette_du_pret(candidat: dict, libelles: dict) -> str:
    """"lent by <e-mail>[ via the team <name>]" — how a refusal names a loan."""
    via = candidat.get("via_group")
    return (f"lent by {libelles[candidat['lent_by']]}"
            + (f" via the team {via.get('name') or '#' + str(via['id'])}" if via else ""))


def pret_a_servir(candidats: list[dict], nom: Optional[str], *, quoi: str) -> Optional[dict]:
    """The rule, on the candidates of ONE resolution (`quoi` names what is resolved, in
    the refusals: "Outlook", "the `zoho` key"…).

    Returns the LENT candidate to serve — the one `nom` (`_account=`) designates — or
    None when no loan is to be served: the connector's ordinary resolution then serves
    what is not lent (own, the org's or the team's own), unchanged. Raises:
    - `account_required` — no name, nothing that is not lent, at least one loan: the
      loans are listed with their lender (and team), nothing is served;
    - `instance_required` — the name designates loans of several lenders.
    A name that designates a non-lent candidate leaves it to the ordinary resolution
    (an own account of the same name wins)."""
    propres = [c for c in candidats if not c.get("lent_by")]
    pretes = [c for c in candidats if c.get("lent_by")]
    if nom:
        if any(c["account"] == nom for c in propres):
            return None
        choix = [c for c in pretes if c["account"] == nom]
        if len(choix) > 1:
            libelles = libelles_preteurs(c["lent_by"] for c in choix)
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(f"Several colleagues lent you an account named `{nom}` for {quoi} ("
                         + ", ".join(libelles[c["lent_by"]] for c in choix)
                         + "): pass `_instance=` with the ref of the one to use "
                         "(oto_instance op='list'). Nothing was done."),
                data={"code": "instance_required", "retryable": False}))
        return choix[0] if choix else None
    if propres or not pretes:
        return None
    libelles = libelles_preteurs(c["lent_by"] for c in pretes)
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"You have no account of your own for {quoi}. A lent account is used "
                 "only when you name it: pass `_account=` with one of "
                 + ", ".join(f"`{c['account']}` ({etiquette_du_pret(c, libelles)})"
                             for c in pretes)
                 + ", or connect your own account. Nothing was done."),
        data={"code": "account_required", "retryable": False,
              "lent_accounts": [{"account": c["account"],
                                 "lent_by": libelles[c["lent_by"]],
                                 **({"via_group": c["via_group"]}
                                    if c.get("via_group") else {})}
                                for c in pretes]}))


def ligne_d_un_membre(row) -> bool:
    """Is the vault row that served (`credential_row` of the call trace) a MEMBER row —
    the only kind a person lends? Pure: the echo asks it on the event loop, and only a
    member row is worth a query."""
    return bool(row) and row[0] == credentials_store.MEMBER


def preteur_de_la_ligne(sub: Optional[str], row) -> Optional[str]:
    """The sub of the member whose row served, when it is not `sub`'s: a loan. None =
    her own row (in any org), a team or org key, or no keyed resolution. Pure."""
    if not ligne_d_un_membre(row):
        return None
    _, _, proprietaire = str(row[1]).partition(":")
    return proprietaire if proprietaire and proprietaire != sub else None


def equipe_du_pret(sub: str, row) -> Optional[dict]:
    """The team (`{id, name}`) through which the row is lent to `sub` — None when it is
    lent to her by name (`user:<sub>`), which prevails."""
    from .. import group_store, providers
    entity_type, entity_id, connector, account = row
    _, side = credentials_store.get_instance_sharing(
        entity_type, entity_id, providers.credential_provider(connector), account)
    if f"user:{sub}" in (side or []):
        return None
    for s in side or []:
        kind, _, gid = str(s).partition(":")
        if kind == "group" and gid.isdigit() and group_store.is_group_member(sub, int(gid)):
            g = group_store.get_group(int(gid)) or {}
            return {"id": int(gid), "name": g.get("name")}
    return None


def rappel_du_compte(sub: Optional[str], account: str, row) -> dict:
    """The descriptor a response carries: `{_account}` for an account of one's own (or
    of the org, of the team), plus `lent_by` (the lender's label) and, for a loan to a
    team, `lent_via_group` (`{id, name}`). Reads the base only for a loan."""
    out = {"_account": account}
    preteur = preteur_de_la_ligne(sub, row)
    if preteur is None:
        return out
    out["lent_by"] = libelles_preteurs([preteur])[preteur]
    equipe = equipe_du_pret(sub, row)
    if equipe:
        out["lent_via_group"] = equipe
    return out

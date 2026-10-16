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

A candidate is a dict `{account, lent_by?, via_group?, via_org?, …}`: `lent_by` = the
sub of the lender (absent = not a loan), `via_group` = `{id, name}` of the borrowing team,
`via_org` = `{id, name}` of the org it is lent to as a whole (both absent = lent to the
caller by name). The connector keeps whatever else it needs on it.

Wired: Microsoft (`auth/microsoft._lent_for_call`, fed by `prets_de`), the cascade's team
and org rungs for every other connector (`access/resolve._group_fetch`/`_org_fetch`: a
loan to the team or to the org is never picked automatically), the access-level guard
(`connectors/access_level_gate`: a designated loan is judged under its lender's org and
its borrowing team) and the echo (every connector: the descriptor is read from the row
that served).
"""
from __future__ import annotations

from typing import Iterable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, db, instance_refs, providers
from ..mcp_errors import McpError

#: The carriers whose account AUTHORIZES its services one by one (incremental consent):
#: a loan of their account lends a LIST of services, frozen at the lend
#: (`connector_credentials.share_services`, per borrower). A service the lender
#: authorizes later is never lent implicitly; a borrower with no list for it is
#: refused, never served "everything" (`exiger_service_prete`).
PORTEURS_A_SERVICES = frozenset({"google", "microsoft"})
CODE_SERVICE_NON_PRETE = "service_not_lent"


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
    """"lent by <e-mail>[ via the team <name>| to the whole org <name>]" — how a refusal
    names a loan."""
    via, org = candidat.get("via_group"), candidat.get("via_org")
    return (f"lent by {libelles[candidat['lent_by']]}"
            + (f" via the team {via.get('name') or '#' + str(via['id'])}" if via else "")
            + (f" to the whole org {org.get('name') or '#' + str(org['id'])}" if org else ""))


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
    # An unnamed account (single-key connector) has no `_account=` to pass: its ref.
    nommes = [c for c in pretes if c["account"]]
    sans_nom = [c for c in pretes if not c["account"]]
    gestes = []
    if nommes:
        gestes.append("pass `_account=` with one of " + ", ".join(
            f"`{c['account']}` ({etiquette_du_pret(c, libelles)})" for c in nommes))
    if sans_nom:
        gestes.append("pass `_instance=` with one of " + ", ".join(
            f"`{c['ref']}` ({etiquette_du_pret(c, libelles)})" for c in sans_nom))
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"You have no account of your own for {quoi}. A lent account is used "
                 "only when you name it: " + ", or ".join(gestes)
                 + ", or connect your own account. Nothing was done."),
        data={"code": "account_required", "retryable": False,
              "lent_accounts": [{"account": c["account"],
                                 "lent_by": libelles[c["lent_by"]],
                                 **({"via_group": c["via_group"]}
                                    if c.get("via_group") else {}),
                                 **({"via_org": c["via_org"]} if c.get("via_org") else {}),
                                 **({"instance": c["ref"]} if not c["account"] else {})}
                                for c in pretes]}))


def _candidats(lents: list[dict], porteur: str, voie: dict) -> list[dict]:
    """Live loans (`group_store.lent_instances`/`live_loans`) as candidates of the rule:
    `{account, lent_by, lender_org, entity_id, ref, meta}` plus the way (`voie`)."""
    return [{"account": i["account"], "lent_by": i["lender"],
             "lender_org": int(str(i["entity_id"]).partition(":")[0]),
             "entity_id": i["entity_id"], "meta": i["meta"], **voie,
             "ref": instance_refs.ref_for_credential(
                 credentials_store.MEMBER, i["entity_id"], porteur, i["account"])}
            for i in lents]


def prets_de_l_equipe(group_id: int, porteur: str) -> list[dict]:
    """The live loans of `porteur` to a team (`group_store.lent_instances`: same org,
    lender still a member and awake, instance not suspended, personal key not forbidden)
    as candidates of the rule, with `via_group`. Reads the team's name only when a loan
    exists."""
    from .. import group_store
    lents = group_store.lent_instances(group_id, porteur)
    if not lents:
        return []
    equipe = {"id": int(group_id), "name": (group_store.get_group(group_id) or {}).get("name")}
    return _candidats(lents, porteur, {"via_group": equipe})


def prets_de_l_org(org_id: int, porteur: str, sub: str) -> list[dict]:
    """The live loans of `porteur` to the whole org (`share_side` `org:<id>`, same
    liveness as a team loan) that serve `sub`: a member of that org, her own row
    excepted. Candidates with `via_org`; membership and the org's name are read only when
    a loan exists."""
    from .. import group_store, org_store, roles
    mien = credentials_store.member_id(org_id, sub)
    lents = [i for i in group_store.live_loans(f"org:{org_id}", int(org_id), porteur)
             if i["entity_id"] != mien]
    if not lents or not roles.is_org_member(sub, int(org_id)):
        return []
    org = {"id": int(org_id), "name": (org_store.get_org(int(org_id)) or {}).get("name")}
    return _candidats(lents, porteur, {"via_org": org})


def prets_de(sub: str, porteur: str) -> list[dict]:
    """Every loan of `porteur` to `sub`, as candidates of the rule (same shape as
    `prets_de_l_equipe`, `via_group`/`via_org` only for a loan to a team or to the org):
    lent to her by name (any org, the named loan is the consent; `set_at` of the row), to
    a team of hers or to the whole of her context org. A row lent to her reads as lent to
    her, then to her team, then to the org. The reach of the Microsoft resolver
    (`auth/microsoft.lent_accounts`) and of the access-level guard."""
    from .. import group_store
    from . import scope
    org = scope.current_org(sub)
    mien = credentials_store.member_id(org, sub) if org is not None else None
    out, vus = [], set()
    for r in credentials_store.list_shared_with([f"user:{sub}"]):
        if (r["connector"] != porteur or r["entity_type"] != credentials_store.MEMBER
                or r["entity_id"] == mien):
            continue
        lender_org, _, lender = str(r["entity_id"]).partition(":")
        if not (lender_org.isdigit() and lender):
            continue
        vus.add((r["entity_id"], r["account"] or ""))
        out.append({"account": r["account"] or "", "lent_by": lender,
                    "lender_org": int(lender_org), "entity_id": r["entity_id"],
                    "meta": dict(r.get("meta") or {}), "set_at": r.get("set_at"),
                    "ref": instance_refs.ref_for_credential(
                        credentials_store.MEMBER, r["entity_id"], porteur,
                        r["account"] or "")})
    partages = [prets_de_l_equipe(int(g["group_id"]), porteur)
                for g in (group_store.list_groups_for_user(sub, org) if org is not None
                          else [])]
    if org is not None:
        partages.append(prets_de_l_org(org, porteur, sub))
    for candidats in partages:
        for c in candidats:
            if c["entity_id"] == mien or (c["entity_id"], c["account"]) in vus:
                continue
            vus.add((c["entity_id"], c["account"]))
            out.append(c)
    return out


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


def cibles_qui_servent(sub: str, side) -> list[str]:
    """The entries of a `share_side` through which the row is lent to `sub`, in the
    order that prevails: to her by name (`user:<sub>`), then a team of hers, then the
    whole of an org of hers."""
    from .. import group_store, roles
    side = [str(s) for s in side or []]
    out = [s for s in side if s == f"user:{sub}"]
    for s in side:
        kind, _, ident = s.partition(":")
        if kind == "group" and ident.isdigit() and group_store.is_group_member(sub, int(ident)):
            out.append(s)
    for s in side:
        kind, _, ident = s.partition(":")
        if kind == "org" and ident.isdigit() and roles.is_org_member(sub, int(ident)):
            out.append(s)
    return out


def voie_du_pret(sub: str, row) -> Optional[tuple[str, dict]]:
    """How the row is lent to `sub`: `("group", {id, name})` through a team of hers,
    `("org", {id, name})` to the whole of an org of hers, None when it is lent to her by
    name (`user:<sub>`), which prevails — then a team, then the org."""
    from .. import group_store, org_store
    entity_type, entity_id, connector, account = row
    _, side = credentials_store.get_instance_sharing(
        entity_type, entity_id, providers.credential_provider(connector), account)
    cibles = cibles_qui_servent(sub, side)
    if not cibles or cibles[0] == f"user:{sub}":
        return None
    kind, _, ident = cibles[0].partition(":")
    if kind == "group":
        g = group_store.get_group(int(ident)) or {}
        return "group", {"id": int(ident), "name": g.get("name")}
    o = org_store.get_org(int(ident)) or {}
    return "org", {"id": int(ident), "name": o.get("name")}


def exiger_service_prete(sub: Optional[str], service: str, servi) -> None:
    """A LENT Google or Microsoft account serves `service` only if the loan lends it.

    `servi` = the resolution that served (`entity_type`, `entity_id`, `account` of its
    row), read only for a service of such an account. No-op for one's
    own row, a team or org key, a carrier outside `PORTEURS_A_SERVICES`, and a call to
    the account itself (`google`, `microsoft`: identity only). The services lent to
    `sub` are the union of the lists of every entry that reaches her
    (`cibles_qui_servent`); an entry without a list lends none. Refused, naming the
    lender, the services the loan covers and how to ask for more — nothing is
    served instead."""
    porteur = providers.credential_provider(service)
    if porteur not in PORTEURS_A_SERVICES or service == porteur:
        return
    entity_type, entity_id, account = servi.entity_type, servi.entity_id, servi.account or ""
    preteur = preteur_de_la_ligne(sub, (entity_type, entity_id))
    if preteur is None:
        return
    _, side = credentials_store.get_instance_sharing(entity_type, entity_id, porteur,
                                                     account or "")
    par_cible = credentials_store.services_pretes(entity_type, entity_id, porteur,
                                                  account or "")
    couverts = sorted({s for c in cibles_qui_servent(sub, side) for s in par_cible.get(c, [])})
    if service in couverts:
        return
    qui = libelles_preteurs([preteur])[preteur]
    label = providers.REGISTRY[porteur].label
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"The {label} account `{account}` lent to you by {qui} does not lend "
                 f"`{service}`: this loan covers "
                 + (", ".join(f"`{s}`" for s in couverts) if couverts else "no service")
                 + f". Ask {qui} to extend it (oto_instance op='lend', connector="
                 f"'{porteur}', the same borrower, `services` with `{service}` added). "
                 "Nothing was done."),
        data={"code": CODE_SERVICE_NON_PRETE, "retryable": False, "service": service,
              "lent_by": qui, "services_lent": couverts}))


def equipe_du_pret(sub: str, row) -> Optional[dict]:
    """The team (`{id, name}`) through which the row is lent to `sub` — None when it is
    lent to her by name or to the whole org (`voie_du_pret`)."""
    voie = voie_du_pret(sub, row)
    return voie[1] if voie and voie[0] == "group" else None


def rappel_du_compte(sub: Optional[str], account: str, row) -> dict:
    """The descriptor a response carries: `{_account}` for an account of one's own (or
    of the org, of the team), plus `lent_by` (the lender's label) and, for a loan to a
    team, `lent_via_group` (`{id, name}`), to the whole org, `lent_via_org`. Reads the
    base only for a loan."""
    out = {"_account": account}
    preteur = preteur_de_la_ligne(sub, row)
    if preteur is None:
        return out
    out["lent_by"] = libelles_preteurs([preteur])[preteur]
    voie = voie_du_pret(sub, row)
    if voie:
        out["lent_via_group" if voie[0] == "group" else "lent_via_org"] = voie[1]
    return out


class PretsDuParcours:
    """The rule along ONE walk of the cascade (`access/resolve`), for every connector the
    team and org rungs serve: what those rungs set aside without a name, and the loan a
    name served. Created per resolution, never shared."""

    def __init__(self):
        self.ecartes: dict = {}
        self.servis: dict = {}

    def au_palier_equipe(self, group_id: int, porteur: str, nom: Optional[str], *,
                         multi: bool) -> tuple[bool, Optional[tuple]]:
        """`(decided, hit)` at a team's rung (`_au_palier`)."""
        return self._au_palier("group", str(group_id), prets_de_l_equipe(group_id, porteur),
                               porteur, nom, multi=multi)

    def au_palier_org(self, org_id: int, porteur: str, sub: str, nom: Optional[str], *,
                      multi: bool) -> tuple[bool, Optional[tuple]]:
        """`(decided, hit)` at the org's rung, for the loans to the whole org that serve
        `sub` (`_au_palier`)."""
        return self._au_palier("org", str(org_id), prets_de_l_org(org_id, porteur, sub),
                               porteur, nom, multi=multi)

    def _au_palier(self, palier: str, eid: str, pretes: list[dict], porteur: str,
                   nom: Optional[str], *, multi: bool) -> tuple[bool, Optional[tuple]]:
        """Not decided: no loan, or the tier's own accounts resolve as before (a loan is
        never picked automatically). Decided: the named loan, `(key, account)`, or None —
        unnamed loans set aside when the tier holds nothing of its own, the hand passing
        to the next rung."""
        if not pretes:
            return False, None
        if multi:
            propres = [a["account"] for a in credentials_store.list_accounts(
                palier, eid, porteur)]
        else:
            propres = [""] if credentials_store.has_credential(palier, eid, porteur) else []
        if not nom:
            if propres:
                return False, None
            self.ecartes.update({(c["entity_id"], c["account"]): c for c in pretes})
            return True, None
        pret = pret_a_servir([{"account": a} for a in propres] + pretes, nom,
                             quoi=f"`{porteur}`")
        if pret is None:
            return False, None
        key = credentials_store.get_credential(credentials_store.MEMBER, pret["entity_id"],
                                               porteur, pret["account"])
        if not key:
            return True, None
        self.servis[(palier, eid, pret["account"])] = pret
        return True, (key, pret["account"])

    def refuser_si_seuls(self, win, porteur: str) -> None:
        """Unnamed loans were set aside and nothing of the org's or the team's own won
        (nothing, or the platform key): `account_required`, naming them and their
        lender — never the platform key in their place."""
        if self.ecartes and (win is None or win.mode == "platform"):
            pret_a_servir(list(self.ecartes.values()), None, quoi=f"`{porteur}`")

    def ligne_servie(self, win) -> tuple:
        """`(entity_type, entity_id)` of the row that served the winning rung: the
        LENDER's when a named loan won the team or org rung — the trace, the echo
        (`lent_by`) and a token rotation name it, not the team's or the org's."""
        pret = self.servis.get((win.mode, win.entity_id, win.account or ""))
        return ((credentials_store.MEMBER, pret["entity_id"]) if pret
                else (win.entity_type, win.entity_id))

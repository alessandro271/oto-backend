"""HubSpot — pousser les lignes d'un tableau en contacts ou entreprises, PAR RÉFÉRENCE.

Second module du connecteur (`hubspot` tient les objets, listes et propriétés) : il ne
porte que `hubspot_push_rows`. `hubspot_object op=create|update` prenait la fiche en
ARGUMENTS (`properties={"email": …, "firstname": …}`), une personne par appel. Ici
l'agent désigne des lignes ; le serveur les lit, crée ou met à jour, associe, range dans
une liste, écrit en retour l'id HubSpot et l'état sur chaque ligne, et ne rend que des
comptes. La mécanique commune vit dans `datastore/par_reference.py`.

**Rapprocher sans deviner.** Un enregistrement existe-t-il déjà ? On le demande à
HubSpot par la propriété d'unicité (`email` pour un contact, `domain` pour une
entreprise), en UN appel de recherche `IN` pour tout le lot — jamais un appel par ligne
(le plafond d'une app privée est de 190 requêtes / 10 s). Deux enregistrements pour la
même valeur : la ligne échoue (`hubspot_ambiguous_match`) plutôt que d'en choisir un.
Une ligne qui porte déjà son id HubSpot est mise à jour par cet id, sans recherche.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .. import access, session_org
from ..datastore import par_reference as pr
from ..datastore.identite import AdresseJson as Adresse
from .hubspot import _scope_refusal

#: La propriété qui dit « c'est le même enregistrement », par type d'objet.
CLE_PAR_DEFAUT = {"contacts": "email", "companies": "domain"}
CREE, MAJ, ECHEC = "created", "updated", "failed"
#: HubSpot plafonne une page de recherche, et une liste de valeurs `IN`, à 100.
_PAGE = 100


def _cle(v) -> Optional[str]:
    """La valeur de rapprochement, comparée sans casse (HubSpot range emails et
    domaines en minuscules)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    return s or None


def _proprietes(ligne: dict, mapping: dict[str, str]) -> tuple[dict, Optional[str]]:
    props: dict = {}
    for prop, colonne in mapping.items():
        v = pr.valeur(ligne, colonne)
        if v is None:
            continue
        if isinstance(v, bool):
            props[prop] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            props[prop] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            # Une propriété à choix multiples HubSpot s'écrit `a;b;c`.
            props[prop] = ";".join(str(x) for x in v)
        else:
            return {}, "unsupported_value"
    return props, None


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.hubspot.client import HubSpotClient

    def _client() -> HubSpotClient:
        key, _ = access.resolve_api_key("hubspot")
        return HubSpotClient(api_key=key)

    def _existants(c: HubSpotClient, object_type: str, prop: str,
                   valeurs: list[str]) -> dict[str, list[str]]:
        """{valeur: [ids]} pour les valeurs déjà présentes chez HubSpot."""
        trouves: dict[str, list[str]] = {}
        for i in range(0, len(valeurs), _PAGE):
            tranche = valeurs[i:i + _PAGE]
            after = None
            while True:
                page = c.search_objects(
                    object_type, filters=[{"propertyName": prop, "operator": "IN",
                                           "values": tranche}],
                    properties=[prop], limit=_PAGE, after=after) or {}
                for rec in page.get("results") or []:
                    cle = _cle((rec.get("properties") or {}).get(prop))
                    if cle is not None:
                        trouves.setdefault(cle, []).append(str(rec.get("id")))
                after = ((page.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    break
        return trouves

    def _associer(c: HubSpotClient, object_type: str, object_id: str,
                  vers: str, vers_id: str) -> None:
        # L'association PAR DÉFAUT de HubSpot (API v4) : aucun identifiant de type
        # d'association à deviner, c'est HubSpot qui choisit celui du couple d'objets.
        c._request("PUT", f"/crm/v4/objects/{object_type}/{object_id}/associations/"
                          f"default/{vers}/{vers_id}")

    @mcp.tool()
    def hubspot_push_rows(
        datastore: Adresse,
        object_type: Literal["contacts", "companies"],
        field_mapping: dict[str, str],
        row_ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
        match_property: Optional[str] = None,
        associate_with: Optional[Literal["contacts", "companies", "deals"]] = None,
        associate_id_column: Optional[str] = None,
        list_id: Optional[str] = None,
        id_column: str = "hubspot_id",
        status_column: str = "hubspot_status",
        batch_size: int = 25,
        dry_run: bool = False,
    ) -> dict:
        """Create or update HubSpot contacts/companies FROM DATASTORE ROWS — the bulk
        way; nothing personal goes through this call.

        Name the rows (`row_ids`, or a `filter`); oto reads them, finds existing
        records by `match_property` (one search for the whole batch), creates or
        updates, optionally associates each record and adds it to a list, and writes
        back `id_column` (the HubSpot id) and `status_column` (created | updated |
        failed) on each row. The answer is counts plus `errors: [{row_id, code}]` —
        never a value read from a row.

        By `filter`, only rows whose `status_column` is empty are taken: call again
        with the same filter until `remaining` is 0; name a row in `row_ids` to retry
        it. A row that already carries its HubSpot id is updated by that id. Two
        HubSpot records for one match value fail the row (`hubspot_ambiguous_match`)
        rather than picking one. `dry_run` reads and checks, sends nothing.

        Args:
            datastore: the table, by name or number.
            object_type: contacts | companies.
            field_mapping: {HubSpot INTERNAL property name: column} — names from
                hubspot_property (`firstname`, not "First name").
            row_ids: the rows to push (at most 50). Exclusive with `filter`.
            filter: data_rows filter grammar; `{}` = every row not yet treated.
            match_property: the property that identifies a record (default `email`
                for contacts, `domain` for companies); it must be in `field_mapping`.
            associate_with: object type to associate each record with.
            associate_id_column: column holding the HubSpot id to associate with
                (with `associate_with`).
            list_id: a MANUAL or SNAPSHOT list to add every pushed record to.
            id_column: where the HubSpot id is written back.
            status_column: where created | updated | failed is written back; the
                code of a failure goes in its `comment` layer.
            batch_size: rows per call with `filter` (1-50).
            dry_run: read and check only — no HubSpot write, nothing written back.
        """
        mapping = pr.valider_correspondance(field_mapping)
        prop_cle = match_property or CLE_PAR_DEFAUT[object_type]
        if prop_cle not in mapping:
            raise pr.refus("hubspot_match_unmapped",
                           f"`{prop_cle}` (la propriété de rapprochement) doit figurer "
                           "dans `field_mapping`. Rien n'a été envoyé.")
        if (associate_with is None) != (associate_id_column is None):
            raise pr.refus("hubspot_association_incomplete",
                           "`associate_with` et `associate_id_column` vont ensemble. "
                           "Rien n'a été envoyé.")
        lot = pr.ouvrir(datastore, row_ids=row_ids, filter=filter,
                        colonne_etat=status_column, limite=batch_size)
        inconnues = pr.colonnes_inconnues(
            lot, [*mapping.values(), *([associate_id_column] if associate_id_column else [])])
        if inconnues:
            raise pr.refus("push_rows_unknown_columns",
                           f"colonnes absentes du tableau : {', '.join(inconnues)}. "
                           "Rien n'a été envoyé.", columns=inconnues)

        recu = pr.Recu()
        # Première passe, sans appel : ce qui part, ce qui est écarté, et pourquoi.
        a_pousser: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        traitees = 0
        for ligne in lot.lignes:
            traitees += 1
            rid = str(ligne.get("_id"))
            if pr.tenue_ailleurs(ligne):
                recu.ecarter(rid, "row_locked")
                continue
            props, code = _proprietes(ligne, mapping)
            if code:
                recu.ecarter(rid, code)
                continue
            connu = pr.valeur(ligne, id_column)
            cle = _cle(props.get(prop_cle))
            if connu is None and cle is None:
                recu.ecarter(rid, "missing_match_value")
                continue
            vers_id = pr.valeur(ligne, associate_id_column) if associate_id_column else None
            a_pousser.append((rid, props, str(connu) if connu is not None else None,
                              str(vers_id) if vers_id is not None else None))

        if dry_run:
            recu.compter("would_push", len(a_pousser))
            return recu.rendre(lot, selectionnees=len(lot.lignes), dry_run=True,
                               traitees=traitees, object_type=object_type,
                               match_property=prop_cle,
                               written_back={"id_column": id_column,
                                             "status_column": status_column})

        c = _client() if a_pousser else None
        pousses: list[str] = []
        try:
            if c is not None and list_id:
                fiche = c.get_list(list_id) or {}
                if (fiche.get("list") or fiche).get("processingType") == "DYNAMIC":
                    raise pr.refus("hubspot_list_dynamic",
                                   f"la liste {list_id} est DYNAMIC : ses membres sont "
                                   "recalculés par HubSpot. Rien n'a été envoyé.")
            a_chercher = sorted({_cle(p.get(prop_cle)) for _, p, connu, _ in a_pousser
                                 if connu is None} - {None})
            existants = _existants(c, object_type, prop_cle, a_chercher) if a_chercher else {}
        except UpstreamHTTPError as e:
            scope = _scope_refusal(e, object_type)
            if scope is not None:
                raise scope from None
            raise

        # Une ligne non envoyée (budget, arrêt) n'est pas « traitée » : elle reste.
        traitees -= len(a_pousser)
        for rid, props, connu, vers_id in a_pousser:
            if recu.budget_epuise():
                recu.arret = "time_budget"
                break
            traitees += 1
            cible = connu
            if cible is None:
                ids = existants.get(_cle(props.get(prop_cle)), [])
                if len(ids) > 1:
                    recu.echec(rid, "hubspot_ambiguous_match")
                    pr.ecrire(lot, rid, {status_column: ECHEC,
                                         f"{status_column}.comment": "hubspot_ambiguous_match"})
                    continue
                cible = ids[0] if ids else None
            try:
                if cible is None:
                    rec = c.create_object(object_type, props) or {}
                    cible, etat = str(rec.get("id") or ""), CREE
                    if not cible:
                        recu.echec(rid, "hubspot_not_created")
                        pr.ecrire(lot, rid, {status_column: ECHEC,
                                             f"{status_column}.comment": "hubspot_not_created"})
                        continue
                else:
                    c.update_object(object_type, cible, props)
                    etat = MAJ
                if associate_with and vers_id:
                    _associer(c, object_type, cible, associate_with, vers_id)
                    recu.compter("associated")
            except UpstreamHTTPError as e:
                statut = getattr(e, "status_code", None)
                if _scope_refusal(e, object_type) is not None:
                    recu.arret, traitees = "hubspot_missing_scopes", traitees - 1
                    break
                if statut in (401, 429):
                    recu.arret = "rate_limited" if statut == 429 else "hubspot_http_401"
                    traitees -= 1
                    break
                code = "hubspot_conflict" if statut == 409 else f"hubspot_http_{statut}"
                recu.echec(rid, code)
                ecrit = pr.ecrire(lot, rid, {status_column: ECHEC,
                                             f"{status_column}.comment": code})
                if ecrit:
                    recu.echec(rid, f"writeback_{ecrit}")
                continue
            recu.compter(etat)
            pousses.append(cible)
            ecrit = pr.ecrire(lot, rid, {
                id_column: cible, f"{id_column}.comment": f"hubspot {object_type}",
                status_column: etat})
            if ecrit:
                # L'enregistrement EXISTE chez HubSpot : ce code dit que la ligne ne le sait pas.
                recu.echec(rid, f"writeback_{ecrit}")

        if list_id and pousses:
            try:
                for i in range(0, len(pousses), _PAGE):
                    c.add_list_memberships(list_id, pousses[i:i + _PAGE])
                recu.compter("added_to_list", len(pousses))
            except UpstreamHTTPError as e:
                recu.arret = recu.arret or f"hubspot_list_http_{getattr(e, 'status_code', None)}"

        # La ligne FACTURÉE compte les enregistrements écrits, comme N appels à
        # hubspot_object l'auraient fait (`tool_calls.quantity`).
        session_org.note_call_trace(quantity=len(pousses))
        return recu.rendre(lot, selectionnees=len(lot.lignes), dry_run=False,
                           traitees=traitees, object_type=object_type,
                           match_property=prop_cle,
                           written_back={"id_column": id_column,
                                         "status_column": status_column})

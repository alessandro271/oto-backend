"""HelloStock administration — les trois gestes qui AGISSENT sur la marketplace de
production.

Trois outils, un par geste, parce qu'aucun ne partage ses paramètres avec une
lecture (ADR 0047 : paramètres disjoints → le NOM porte l'avertissement) — et parce
que `status`, filtre d'une liste, deviendrait ici la valeur écrite : le même mot
avec deux sens dans une même surface.

- `hellostock_demande_send` **envoie un courriel** à des membres réels. C'est le
  seul geste dont l'effet atteint une personne : il est en **dry-run PAR DÉFAUT**
  (convention du dépôt pour ce qui part chez un tiers), et l'aperçu nomme les
  destinataires, ceux qui l'ont déjà reçue, et ce que le courriel contient.
- `hellostock_demande_set_status` et `hellostock_offre_update` changent ce que la
  marketplace AFFICHE : seul `published` est visible publiquement, et un mot-clé
  d'offre alimente la recherche publique. Aucun courriel ne part. `dry_run`
  disponible, défaut `False`, comme les écritures des autres connecteurs.

**Ce que ce module ajoute au contrat**, parce que l'API ne le fait pas :

- **aucun renvoi en double sans le vouloir** : HelloStock trace chaque envoi mais
  n'en refuse aucun, et n'a pas de clé d'idempotence — un agent qui rejoue son tour
  écrirait deux fois aux mêmes fournisseurs. Un destinataire qui a déjà reçu cette
  demande est refusé, sauf `allow_resend=True` (une relance voulue) ;
- **le message borné ici** : au-delà de 2 000 caractères, le serveur répond « aucun
  destinataire sélectionné », ce qui ferait chercher l'erreur ailleurs ;
- **une écriture se lit AVANT** (l'identifiant inconnu d'une demande rend une 500
  côté serveur, pas un 404) **et se relit APRÈS** pour une offre (le serveur
  normalise les mots-clés : on rend ce qui est stocké, pas ce qui a été demandé).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INTERNAL_ERROR
from oto.tools.hellostock import STATUSES
from pydantic import Field

from ..mcp_errors import McpError
from .hellostock_socle import _bad, _client, _run, traduire

_MESSAGE_MAX = 2000
_DESTINATAIRES_MAX = 50

# Ce que le courriel d'envoi porte, et ce qu'il tait (contrat + gabarit du serveur).
_SPECS = ("matiere", "nuance", "format", "dimensions", "epaisseur", "quantite",
          "delai", "certificatRequis")
_VISIBILITE = ("`published` est visible sur la marketplace publique immédiatement ; "
               "tout autre statut la retire des pages publiques. Aucun courriel ne part.")


def _destinataires(user_ids: list[int]) -> list[int]:
    if not user_ids:
        raise _bad("`user_ids` : au moins un membre destinataire (ids de hellostock_membre).")
    if len(user_ids) > _DESTINATAIRES_MAX:
        raise _bad(f"`user_ids` : {_DESTINATAIRES_MAX} destinataires au plus par envoi "
                   f"({len(user_ids)} reçus).")
    doublons = sorted({u for u in user_ids if user_ids.count(u) > 1})
    if doublons:
        raise _bad(f"`user_ids` contient des doublons : {doublons}.")
    return list(user_ids)


def _deja_recus(demande: dict) -> dict[int, str]:
    """`{user_id: date du dernier envoi}` d'après les envois tracés de la demande."""
    out: dict[int, str] = {}
    for e in demande.get("envois") or []:
        uid = e.get("userId")
        if isinstance(uid, int) and uid not in out:  # du plus récent au plus ancien
            out[uid] = e.get("sentAt")
    return out


def _avertissements(demande: dict, ids: list[int]) -> list[str]:
    """Des FAITS sur l'envoi, pas des refus : l'administrateur tranche."""
    out = []
    if demande.get("status") != "published":
        out.append(f"statut `{demande.get('status')}` : les liens du courriel ouvrent la "
                   "page publique de la demande, qui n'affiche que les demandes "
                   "publiées — les destinataires tomberont sur « Demande indisponible » "
                   "tant qu'elle ne l'est pas.")
    acheteur = (demande.get("contact") or {}).get("userId")
    if acheteur in ids:
        out.append(f"le membre {acheteur} est l'acheteur qui a déposé cette demande.")
    return out


def _apercu_destinataires(client, ids: list[int]) -> tuple[list[dict], list[int]]:
    """Qui recevrait le courriel — et les ids qui ne sont pas des membres (l'envoi
    réel serait refusé en entier : HelloStock vérifie tous les ids avant d'envoyer)."""
    from oto.tools.common.errors import UpstreamHTTPError

    trouves, inconnus = [], []
    for uid in ids:
        try:
            m = client.get_user(uid)
        except UpstreamHTTPError as e:
            if e.status_code == 404:
                inconnus.append(uid)
                continue
            raise traduire(e) from None
        trouves.append({k: m.get(k) for k in
                        ("id", "name", "company", "email", "location", "services")})
    return trouves, inconnus


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def hellostock_demande_send(
        demande_id: int,
        user_ids: list[int],
        message: Optional[str] = None,
        allow_resend: bool = False,
        dry_run: bool = True,
    ) -> dict:
        """SEND a HelloStock demande by EMAIL to chosen members — real people, from
        HelloStock's address, recorded in the admin's name. IRREVERSIBLE once sent.

        **dry_run is True by default**: nothing is sent; the tool returns what would
        go out — the specs, the recipients (name, company, email), who already
        received this demande, and warnings. Show it to the human; send only on
        their go, with `dry_run=False`.

        The email carries the specs (material, grade, format, dimensions, thickness,
        quantity, deadline, certificate) and links to the demande's page — never
        the buyer's identity, reference or comment. `message` goes in verbatim.

        Result of a real send: `envoyes` (count), `echecs` (emails that failed,
        not recorded), `noop` (true = HelloStock's mailer is not configured: NO
        email left, yet the sends were recorded as done).

        Args:
            demande_id: the demande to send.
            user_ids: recipients, 1-50 member ids (`hellostock_membre`).
            message: optional note put verbatim in the email, 2000 characters max.
            allow_resend: send again to members who already received this demande (a deliberate reminder).
            dry_run: default True = preview only. Pass False to send.
        """
        ids = _destinataires(user_ids)
        if message is not None and len(message) > _MESSAGE_MAX:
            raise _bad(f"`message` : {_MESSAGE_MAX} caractères au plus "
                       f"({len(message)} reçus) — rien n'est parti.")
        client = _client()
        demande = _run(lambda: client.get_demande(demande_id))
        deja = _deja_recus(demande)
        deja_servis = [u for u in ids if u in deja]
        avert = _avertissements(demande, ids)

        if dry_run:
            recipients, inconnus = _apercu_destinataires(client, ids)
            if inconnus:
                avert.append(f"ids qui ne sont pas des membres : {inconnus} — l'envoi "
                             "réel serait refusé en entier.")
            bloque = bool(deja_servis) and not allow_resend
            return {
                "dry_run": True,
                "demande": {"id": demande.get("id"), "status": demande.get("status"),
                            **{k: demande.get(k) for k in _SPECS},
                            "fichiers": len(demande.get("fichiers") or [])},
                "recipients": recipients,
                "already_sent": [{"user_id": u, "sent_at": deja[u]} for u in deja_servis],
                "message": message,
                "warnings": avert,
                "note": ("Rien n'est parti. dry_run=False écrit à "
                         f"{len(recipients)} membre(s) réel(s), sans retour possible"
                         + (" — refusé tant que `already_sent` n'est pas vide, sauf "
                            "allow_resend=True." if bloque else ".")),
            }

        if deja_servis and not allow_resend:
            raise _bad(
                "Déjà reçue par " + ", ".join(f"{u} (le {deja[u]})" for u in deja_servis)
                + " : rien n'est parti. Retire ces membres de `user_ids`, ou passe "
                "`allow_resend=True` si c'est une relance voulue.")
        from oto.tools.common.errors import UpstreamHTTPError
        try:
            out = client.send_demande(demande_id, ids, message=message)
        except ValueError as e:
            raise _bad(str(e)) from None
        except UpstreamHTTPError as e:
            if e.status_code == 502:
                echecs = e.body.get("echecs") if isinstance(e.body, dict) else None
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=(
                    "HelloStock n'a pu envoyer aucun courriel (502) : aucun envoi n'a "
                    f"été enregistré. Échecs : {echecs or 'non détaillés'}. Ne relance "
                    "pas en boucle — préviens un administrateur HelloStock."))) from None
            raise traduire(e) from None
        notes = []
        if out.get("noop"):
            notes.append("noop=true : la messagerie de HelloStock n'est pas configurée — "
                         "AUCUN courriel n'est parti, et pourtant les envois sont "
                         "enregistrés dans la demande.")
        if out.get("echecs"):
            notes.append(f"{len(out['echecs'])} courriel(s) en échec, non enregistrés : "
                         f"{out['echecs']}.")
        return {**out, "demande_id": demande_id, "user_ids": ids, "warnings": avert,
                **({"note": " ".join(notes)} if notes else {})}

    @mcp.tool()
    def hellostock_demande_set_status(
        demande_id: int,
        status: Literal[STATUSES],
        dry_run: bool = False,
    ) -> dict:
        """⚠️ WRITES on the live HelloStock marketplace: set a demande's status.

        Any transition is accepted (closed → published included). `published` is
        visible on the public marketplace at once; any other status takes it off
        the public pages. No email is sent. The current status is read first and
        returned as `from`; `dry_run=True` writes nothing.

        Args:
            demande_id: the demande.
            status: the new status.
            dry_run: True = show the transition, write nothing.
        """
        client = _client()
        avant = _run(lambda: client.get_demande(demande_id)).get("status")
        base = {"demande_id": demande_id, "from": avant, "to": status}
        if avant == status:
            return {**base, "unchanged": True, "note": "déjà dans ce statut, rien écrit."}
        if dry_run:
            return {**base, "dry_run": True, "effect": _VISIBILITE}
        return {**_run(lambda: client.update_demande_status(demande_id, status)),
                **base, "effect": _VISIBILITE}

    @mcp.tool()
    def hellostock_offre_update(
        offre_id: int,
        status: Optional[Literal[STATUSES]] = None,
        keywords: Optional[Annotated[list[str], Field(min_length=1, max_length=30)]] = None,
        dry_run: bool = False,
    ) -> dict:
        """⚠️ WRITES on the live HelloStock marketplace: an offre's status and/or its
        keywords.

        `keywords` REPLACES the whole list and is PUBLIC (it feeds the marketplace
        search). Only material designations and specs belong there (`304L`,
        `1.4307`, `X2CrNi18-9`, `EN 10204 3.1`, `3 mm`) — never a steelmaker's
        name, a heat or order number, even when the certificate check shows them:
        HelloStock refuses the whole list (400, naming the term). It lowercases,
        trims and de-duplicates; the result returns what is actually stored.

        `status`: `published` is visible publicly at once, any other status takes
        the offre off the public pages. No email is sent. `dry_run=True` shows the
        change and writes nothing (it cannot pre-check HelloStock's keyword rules).

        Args:
            offre_id: the offre.
            status: the new status.
            keywords: 1-30 terms, 60 characters max each; replaces the current list.
            dry_run: True = show the change, write nothing.
        """
        if status is None and keywords is None:
            raise _bad("`status` ou `keywords` : au moins l'un des deux.")
        client = _client()
        avant = _run(lambda: client.get_offre(offre_id))
        voulu = {k: v for k, v in (("status", status), ("keywords", keywords))
                 if v is not None}
        base = {"offre_id": offre_id,
                "from": {k: avant.get(k) for k in voulu}, "to": voulu}
        if dry_run:
            return {**base, "dry_run": True}
        _run(lambda: client.update_offre(offre_id, status=status, keywords=keywords))
        apres = _run(lambda: client.get_offre(offre_id))
        return {**base, "success": True,
                "written": {k: apres.get(k) for k in voulu}}

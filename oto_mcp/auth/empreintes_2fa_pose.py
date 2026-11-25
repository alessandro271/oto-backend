"""Poser l'empreinte de double authentification des comptes qui n'en ont pas —
`oto-mcp maintenance empreintes-2fa`.

Le webhook ferme toutes les sessions d'un compte à son PREMIER passage, quand aucune
empreinte n'est posée (`revocation_2fa.py` : la livraison ne permet pas d'exclure un
facteur retiré). Pour que ce premier passage reste l'exception, cette commande pose,
une fois, l'empreinte des comptes existants, telle que la Management API la rend
(`annuaire.facteurs_2fa`, le client du constat), SANS JAMAIS FERMER de session.

- **Qui** : les comptes de l'annuaire primaire (sub sans `:`) qui ont une ligne `users`
  et aucune empreinte, par pages de `PAGE`, lues en lecture seule sous
  `statement_timeout` 15 s (`db.empreintes_2fa.comptes_sans_empreinte`).
- **À blanc par défaut** : lit les facteurs, compte, n'écrit rien.
- `appliquer=True` : pose l'empreinte lue, et SEULEMENT là où aucune n'est posée
  (`poser_si_absente`) : une empreinte posée entre-temps par le webhook n'est jamais
  écrasée. Idempotente : rejouée, il n'y a plus rien à poser.
- **Ce qu'elle affiche** : des nombres. Un compte dont Logto ne rend pas les facteurs
  (compte absent de l'annuaire, refus, délai) est compté `non lus` et journalisé par
  son sub ; aucun secret, aucune adresse.
"""
from __future__ import annotations

import logging

from ..db import empreintes_2fa
from . import annuaire, revocation_2fa

logger = logging.getLogger(__name__)

#: Comptes lus par page.
PAGE = 200


def poser_les_absentes(*, appliquer: bool) -> dict:
    """Le rapport : `{"applique", "comptes", "poses", "deja_posees", "non_lus"}` —
    `comptes` sans empreinte trouvés, `poses` empreintes posées (à blanc : à poser),
    `deja_posees` posées par le webhook entre la lecture et la pose, `non_lus` comptes
    dont Logto n'a pas rendu les facteurs. Une panne de base lève."""
    rapport = {"applique": appliquer, "comptes": 0, "poses": 0, "deja_posees": 0,
               "non_lus": 0}
    apres = ""
    while True:
        page = empreintes_2fa.comptes_sans_empreinte(apres, PAGE)
        if not page:
            break
        apres = page[-1]
        for sub in page:
            rapport["comptes"] += 1
            try:
                valeur = revocation_2fa.empreinte(annuaire.facteurs_2fa(sub))
            except annuaire.LectureInachevee as e:
                rapport["non_lus"] += 1
                logger.warning("empreintes-2fa : facteurs non lus pour %s (%s)", sub, e)
                continue
            if not appliquer:
                rapport["poses"] += 1
            elif empreintes_2fa.poser_si_absente(sub, valeur):
                rapport["poses"] += 1
            else:
                rapport["deja_posees"] += 1
        if len(page) < PAGE:
            break
    return rapport


def lignes_du_rapport(rapport: dict) -> list[str]:
    verbe = "posées" if rapport["applique"] else "à poser (à blanc, rien n'est écrit)"
    lignes = [f"comptes sans empreinte : {rapport['comptes']}",
              f"empreintes {verbe} : {rapport['poses']}",
              f"facteurs non lus chez Logto : {rapport['non_lus']}"]
    if rapport["applique"]:
        lignes.append(f"déjà posées par le webhook entre-temps : {rapport['deja_posees']}")
    return lignes


def reste(rapport: dict) -> bool:
    """Il reste à faire : à blanc, une empreinte à poser ; dans les deux cas, un compte
    non lu."""
    return bool(rapport["non_lus"] or (not rapport["applique"] and rapport["poses"]))

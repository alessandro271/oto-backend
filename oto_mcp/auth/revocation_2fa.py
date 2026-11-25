"""Un changement de double authentification ferme toutes les sessions du compte.

## La règle

Après tout facteur ajouté, retiré ou régénéré (application d'authentification, codes
de secours, clé d'accès), toutes les sessions Logto du compte, leurs autorisations, et
tous ses jetons `oto_` sont fermés — la session qui a fait le geste comprise. Le compte
se reconnecte avec ses facteurs du moment. C'est la fermeture du mot de passe changé
(`revocation_mot_de_passe.fermer_tout`), déclenchée par un autre constat.

## Le constat

Logto n'émet aucun événement propre à la double authentification : un geste de
l'Account API sur un facteur s'annonce par un `User.Data.Updated` qui ne porte que
l'entité utilisateur, comme une retouche de profil. On ne lit donc pas l'événement,
on lit l'ÉTAT : à chaque `User.Data.Updated`, la liste des facteurs du compte
(`annuaire.facteurs_2fa`), réduite à une empreinte — le hachage des couples
`(id, type)`, triés. Les ids changent à l'ajout, au remplacement et à la régénération ;
le compte de codes de secours restants, la date de dernier usage ou le nom d'une clé
n'y entrent pas (s'en servir n'est pas un changement).

L'empreinte est comparée à la dernière posée (`db/empreintes_2fa.py`) :

- **différente** — y compris d'une empreinte d'aucun facteur à un premier facteur :
  fermeture ;
- **aucune posée** (premier passage du compte) : on pose sans fermer, SAUF si un
  facteur a été créé moins de `RECENT_S` avant la livraison — c'est alors très
  probablement lui que la livraison annonce ;
- **identique** : rien.

L'empreinte neuve n'est posée qu'APRÈS une fermeture réussie : une fermeture en échec
laisse l'écart en place, et la relivraison de Logto le retrouve. Fermer deux fois est
sans effet ; une livraison rejouée ne fait donc rien de plus.

Les écritures de facteurs par la Management API n'émettent aucun webhook : la remise à
zéro par un opérateur (`capabilities/users_admin._reset_mfa`) ferme elle-même et pose
l'empreinte d'aucun facteur.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from ..db import empreintes_2fa
from . import annuaire

logger = logging.getLogger(__name__)

#: L'âge maximal d'un facteur, jugé sur son `createdAt` face à celui de la livraison,
#: pour qu'un premier passage sans empreinte posée le prenne pour le geste annoncé.
RECENT_S = 3600


def empreinte(facteurs: list[dict]) -> str:
    """L'empreinte d'une liste de facteurs : SHA-256 des couples `(id, type)` triés."""
    couples = sorted((str(f.get("id")), str(f.get("type"))) for f in facteurs)
    return hashlib.sha256(json.dumps(couples).encode()).hexdigest()


def _instant(v) -> Optional[float]:
    """Un `createdAt` de Logto en secondes : nombre (millisecondes) ou ISO 8601."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def facteur_recent(facteurs: list[dict], livraison: float) -> bool:
    """Un facteur créé moins de `RECENT_S` avant `livraison` (ou après) ?"""
    return any((t := _instant(f.get("createdAt"))) is not None and t >= livraison - RECENT_S
               for f in facteurs)


def compte_vise(evt: dict) -> Optional[str]:
    """Le compte dont un `User.Data.Updated` annonce une retouche, ou None."""
    if evt.get("event") != "User.Data.Updated":
        return None
    return ((evt.get("data") or {}).get("id")
            or (evt.get("params") or {}).get("userId") or None)


def constater(sub: str, livraison: float) -> tuple[bool, str]:
    """Lit les facteurs de `sub` et rend `(changement, empreinte du moment)`. Ne pose
    rien. Lève `annuaire.LectureInachevee` si Logto ne les rend pas."""
    facteurs = annuaire.facteurs_2fa(sub)
    neuve = empreinte(facteurs)
    posee = empreintes_2fa.lire(sub)
    if posee is None:
        return facteur_recent(facteurs, livraison), neuve
    return posee != neuve, neuve


def poser(sub: str, valeur: str) -> None:
    empreintes_2fa.poser(sub, valeur)
    # Les facteurs ont changé (ou viennent d'être constatés) : le verdict de la garde
    # des administrateurs (`garde_admin`) se relit, il n'attend pas la fin de son cache.
    from .. import garde_admin
    garde_admin.oublier(sub)


def poser_aucun_facteur(sub: str) -> None:
    """Pose l'empreinte d'un compte sans facteur (après une remise à zéro)."""
    poser(sub, empreinte([]))

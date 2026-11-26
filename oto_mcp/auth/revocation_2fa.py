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
l'entité utilisateur APRÈS le geste (ni les facteurs, ni l'état d'avant), comme une
retouche de profil. On ne lit donc pas l'événement, on lit l'ÉTAT : la liste des
facteurs du compte (`annuaire.facteurs_2fa`), réduite à une empreinte — le hachage des
couples `(id, type)`, triés. Les ids changent à l'ajout, au remplacement et à la
régénération ; le compte de codes de secours restants, la date de dernier usage ou le
nom d'une clé n'y entrent pas (s'en servir n'est pas un changement).

Trois livraisons du webhook déclenchent ce constat (`compte_vise`) :

- `User.Data.Updated` (`data.id`) — un geste de l'Account API ;
- `PostSignIn` (`userId`, ou `user.id`) — un facteur enrôlé PENDANT la connexion (Logto
  peut l'exiger à la connexion) ne passe par aucun `User.Data.Updated` ;
- `PostRegister` (idem) — l'inscription, qui pose l'empreinte du compte neuf.

L'empreinte est comparée à la dernière posée (`db/empreintes_2fa.py`) :

- **différente** — y compris d'une empreinte d'aucun facteur à un premier facteur :
  fermeture ;
- **aucune posée** (premier passage du compte) : **fermeture**, puis pose. La livraison
  ne dit rien de l'état d'avant : elle ne permet jamais d'exclure qu'un facteur vienne
  d'être RETIRÉ (un facteur ancien, enrôlé par un tiers, que la personne retire). On
  ferme donc toujours, quelle que soit la date de création des facteurs. Seule
  exception : `PostRegister` — un compte qui vient de naître n'a aucune session
  d'avant à fermer ; on pose sans fermer. Pour que ce premier passage reste rare, la
  commande d'exploitant `oto-mcp maintenance empreintes-2fa` pose l'empreinte des
  comptes existants sans rien fermer (`empreintes_2fa_pose.py`) ;
- **identique** : rien.

L'empreinte neuve n'est posée qu'APRÈS une fermeture réussie : une fermeture en échec
laisse l'écart en place, et la relivraison de Logto le retrouve. Fermer deux fois est
sans effet ; une livraison rejouée, ou deux livraisons concurrentes qui voient le même
écart, ne font donc rien de plus.

Les écritures de facteurs par la Management API n'émettent aucun webhook : la remise à
zéro par un opérateur (`capabilities/users_admin._reset_mfa`) ferme elle-même et pose
l'empreinte d'aucun facteur.

Quand le compte est un admin plateforme, une alerte part aussi (courriel au titulaire et
aux autres admins, journal, suivi d'erreurs) : `alerte_facteurs_admin`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from ..db import empreintes_2fa
from . import annuaire

logger = logging.getLogger(__name__)

#: L'inscription : son premier passage pose sans fermer (aucune session d'avant).
INSCRIPTION = "PostRegister"

#: Les livraisons d'une connexion aboutie, qui nomment le compte par `userId`/`user.id`.
_CONNEXIONS = ("PostSignIn", INSCRIPTION)


def empreinte(facteurs: list[dict]) -> str:
    """L'empreinte d'une liste de facteurs : SHA-256 des couples `(id, type)` triés."""
    couples = sorted((str(f.get("id")), str(f.get("type"))) for f in facteurs)
    return hashlib.sha256(json.dumps(couples).encode()).hexdigest()


def compte_vise(evt: dict) -> Optional[str]:
    """Le compte dont la livraison appelle le constat de double authentification, ou
    None : `User.Data.Updated` (`data.id`, `params.userId`), `PostSignIn` et
    `PostRegister` (`userId`, `user.id`)."""
    event = evt.get("event")
    if event == "User.Data.Updated":
        return ((evt.get("data") or {}).get("id")
                or (evt.get("params") or {}).get("userId") or None)
    if event in _CONNEXIONS:
        return evt.get("userId") or (evt.get("user") or {}).get("id") or None
    return None


def constater(sub: str, *, inscription: bool = False) -> tuple[bool, str]:
    """Lit les facteurs de `sub` et rend `(fermer, empreinte du moment)`. Ne pose rien.
    Sans empreinte posée, `fermer` est vrai — sauf à l'inscription (`inscription`).
    Lève `annuaire.LectureInachevee` si Logto ne les rend pas."""
    facteurs = annuaire.facteurs_2fa(sub)
    neuve = empreinte(facteurs)
    posee = empreintes_2fa.lire(sub)
    if posee is None:
        return not inscription, neuve
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

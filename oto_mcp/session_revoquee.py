"""Un jeton émis avant la fermeture de toutes les sessions de son compte est refusé.

**Le trou.** `auth/revocation_mot_de_passe.fermer_tout` (mot de passe changé, double
authentification changée ou remise à zéro) révoque les jetons `oto_` du compte et ferme
ses sessions et autorisations Logto. Deux choses lui échappaient :

- un JWT d'accès Logto déjà émis : vérifié HORS LIGNE (signature, émetteur, audience —
  `server.verify_token`), il restait servi jusqu'à son terme, environ une heure ;
- un jeton `oto_` créé entre la liste et la révocation (`revoquer_jetons`).

**La règle.** `fermer_tout` pose pour le compte un instant « révoqué avant »
(`db/revocations_comptes.py`), jamais reculé. Tout jeton présenté ensuite est jugé sur
sa date d'émission : l'`iat` d'un JWT, la création d'un jeton `oto_`. Émis AVANT
l'instant : refusé (`CODE`), à chaque requête, par un 401 HTTP sur les deux faces (face
MCP : `auth/entree_mcp.py`, avant le transport). Émis après — la reconnexion qui suit — :
servi.

**Coût.** Aucune requête de plus : l'instant est joint à la lecture d'entrée du compte
(`db.get_suspension`, la pause avec), faite une fois par requête — sur la face MCP par la
porte HTTP, dont la garde reprend le résultat —, et la date d'un jeton `oto_`
sort de l'`UPDATE … RETURNING` qui le vérifie (`db.verify_api_token`). Aucun cache, donc
aucune fenêtre de retard entre processus : une fermeture mord à la requête suivante,
quel que soit le processus qui la sert.

**Grain.** L'`iat` d'un JWT est en secondes entières, l'instant en microsecondes : un
jeton émis dans la seconde même de la fermeture, après elle, est refusé aussi (il se
réémet). L'instant vient de l'horloge de la base, l'`iat` de celle de Logto : un décalage
d'horloge déplace la frontière d'autant.

Ce que ce module ne fait PAS : lire la base (la garde d'identité lui passe l'état lu), ni
décider qui ferme (c'est `fermer_tout`).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Le code servi aux deux faces, sans traduction (même règle que `account_suspension`).
CODE = "session_revoked"

#: Le statut REST du refus : l'identité n'est plus établie, le porteur se reconnecte.
STATUT = 401

MESSAGE = ("Cette session a été fermée : toutes les sessions et tous les jetons de ce "
           "compte ont été fermés après un changement de mot de passe ou de double "
           "authentification. Reconnecte-toi ; un jeton d'API se réémet depuis le "
           "tableau de bord.")


def emis_le_du_jeton(jeton) -> float:
    """La date d'émission d'un jeton vérifié (`AccessToken`), en secondes depuis l'époque :
    la création d'un jeton `oto_` (`cree_le`, posé par `server._verify_api_token`), l'`iat`
    d'un JWT. 0 quand elle manque : refusé dès qu'une fermeture a eu lieu (fail-closed)."""
    claims = getattr(jeton, "claims", None) or {}
    if getattr(jeton, "client_id", None) == "oto_api_token":
        cree = claims.get("cree_le")
        return float(cree) if cree is not None else 0.0
    return float(claims.get("iat") or 0)


def refus(sub: Optional[str], entree: Optional[dict],
          emis_le: Optional[float]) -> Optional[str]:
    """Le message du refus si le jeton émis à `emis_le` (secondes depuis l'époque) l'a été
    avant l'instant « révoqué avant » de l'état d'entrée `entree` ; `None` sinon.

    `emis_le` None : le jeton ne porte pas de date (pas un jeton : lien d'upload, travail
    de fond) — rien à juger ici. Un JWT sans `iat` arrive avec 0 : refusé dès qu'un
    instant est posé (fail-closed)."""
    if emis_le is None or not entree:
        return None
    avant = entree.get("revoque_avant")
    if avant is None or float(emis_le) >= float(avant):
        return None
    logger.warning("jeton émis avant la fermeture des sessions refusé : sub=%s", sub)
    return MESSAGE

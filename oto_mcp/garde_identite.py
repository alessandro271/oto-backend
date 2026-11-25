"""La garde d'identité : le point unique où une identité établie peut encore être REFUSÉE.

Toute requête qui agit sous un compte traverse UNE fois cette fonction, avec le sub
CANONIQUE (après le drain d'alias), à chaque requête et sans cache :

| face | porte |
|---|---|
| REST, jeton `oto_` (API ou délégation) | `api.base._authenticate`, branche haute |
| REST, JWT (tableau de bord, quel que soit l'émetteur) | `api.base._authenticate`, branche basse |
| MCP, toute requête (JWT OAuth ou jeton `oto_`) | `AccountSuspendedMiddleware.on_request` |
| lien d'upload signé (porte le sub scellé) | `api.uploads._do_signed_upload` |

Trois refus, dans cet ordre : le **tenant désactivé** (`tenant_desactive`, l'espace entier),
puis le **compte en pause** (`account_suspension`, la personne), puis le **jeton émis avant
la fermeture des sessions du compte** (`session_revoquee`, seulement quand la porte passe
la date d'émission du jeton : REST et MCP). La pause et la date de révocation sortent
d'UNE lecture (`account_suspension.entree`). Les prédicats ne s'appellent nulle part ailleurs — `tests/test_garde_identite_chemins.py` énumère les
chemins et fige les appelants.

Ce qui n'y passe pas, et pourquoi : les secrets de MACHINE (worker `otow_`, identité de
service) ne sont pas des comptes ; l'endpoint MCP anonyme d'un projet publié n'a pas de
sub.
"""
from __future__ import annotations

from typing import Optional

from . import account_suspension, session_revoquee, tenant_desactive


def refus(sub: Optional[str], emis_le: Optional[float] = None
          ) -> Optional[tuple[str, str]]:
    """`(code, message)` si l'identité `sub` ne doit plus être servie, `None` sinon.

    `emis_le` : la date d'émission du jeton présenté (secondes depuis l'époque — `iat`
    d'un JWT, création d'un jeton `oto_`), None quand la porte n'a pas de jeton daté.

    Ne rattrape rien : une panne de lecture REMONTE (fail-closed), comme les
    prédicats qu'elle compose."""
    if (coupe := tenant_desactive.refus(sub)):
        return tenant_desactive.CODE, coupe[0]
    lue = account_suspension.entree(sub)
    if (pause := account_suspension.refus(sub, lue)):
        return account_suspension.CODE, pause[0]
    if (revoque := session_revoquee.refus(sub, lue, emis_le)):
        return session_revoquee.CODE, revoque
    return None


def statut(code: str) -> int:
    """Le statut REST d'un refus de `refus` : 401 pour un jeton émis avant la fermeture
    des sessions (l'identité n'est plus établie, le porteur se reconnecte), 403 sinon
    (l'identité est établie, et refusée)."""
    return session_revoquee.STATUT if code == session_revoquee.CODE else 403

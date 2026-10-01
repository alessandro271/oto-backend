"""« Un credential est-il DISPONIBLE pour moi ? » — par connecteur, sur tout le catalogue.

Né d'oto-backend#1112. L'agent d'un utilisateur lui a dit que son LinkedIn n'était
pas connecté alors qu'il l'était (`linkedin_unipile_account op=status` :
`connected:true, alive:true`). Il n'avait pas appelé l'outil de statut : il avait lu
`state:not_selected` sur la ligne du catalogue, et l'avait compris « non connecté ».
Rien sur la ligne ne disait qu'un compte existait. Deux fois, chez deux utilisateurs.

**Trois axes, jamais confondus** — chacun a sa surface, et aucune ne parle pour l'autre :

- **sélectionné dans la toolbox** : `connectors.selection` (`state`). Gouverne la
  VISIBILITÉ des outils, rien d'autre — un connecteur `not_selected` reste appelable
  par `oto_call` (ADR 0036) ;
- **credential disponible** : CE module. Une clé ou un compte existe pour la personne
  à un palier de la cascade (perso, équipe, org, tenant, plateforme) ;
- **vérifié vivant** : jamais calculé ici. Une session peut être morte chez le
  fournisseur alors que le compte reste lié ; seule une sonde le dit, et elle coûte
  un aller-retour réseau. `next_step` NOMME l'outil qui le vérifie.

**Source unique : `access.status_for`**, le snapshot que `/api/me` sert déjà au
dashboard (sonde de présence PRÉCHARGÉE, une marche en mémoire par connecteur, aucun
déchiffrement). Aucune cascade n'est recalculée ici : le dashboard, `oto_connector` et
`oto_list_my_tools` lisent donc le même fait. C'est ce module, et lui seul, que les
deux surfaces agent appellent — une seconde dérivation rouvrirait la contradiction
d'#1112 (le catalogue d'outils disait tout « installé » pendant que la carte disait
« non sélectionné »).

⚠️ Ce n'est PAS le verdict d'aptitude (`connectors/readiness.py`) : celui-là lit en
plus l'option payante, le quota et le rejet enregistré, connecteur par connecteur
(~244 ms l'unité). Ici on répond à une question plus étroite — « existe-t-il de quoi
s'authentifier ? » — mais sur TOUT le catalogue, parce que c'est sur le catalogue que
l'agent concluait.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import access, providers, status_hints
from . import verify as connector_verify

logger = logging.getLogger(__name__)

# Le credential existe et rien n'est en attente : une clé résout, ou (canal hébergé)
# un compte est lié.
CONNECTED = "connected"
# Une clé résout, mais il reste un geste avant de pouvoir agir (lier son compte
# LinkedIn sur une clé Unipile d'org, autoriser oto…) — `next_step` le dit.
PENDING_STEP = "pending_step"

# `over_quota` est un palier PLATEFORME dont la journée est finie : la clé existe
# (c'est l'axe ici), l'épuisement relève de l'aptitude (`readiness`, lecture ciblée).
_PALIER = {"user": "user", "group": "group", "org": "org", "tenant": "tenant",
           "platform": "platform", "over_quota": "platform"}

# Nature du credential d'un canal hébergé : un COMPTE de la personne (sa session
# LinkedIn, WhatsApp…), pas la clé du fournisseur qui le porte.
HOSTED_ACCOUNT = "hosted_account"


class CredentialPresence(BaseModel):
    """La forme servie de `par_connecteur` — DÉCRIT le `dict` produit ci-dessous (même
    régime que `Capability.Output`), sur la ligne d'`oto_connector op=list` comme sur
    le groupe d'`oto_list_my_tools`. Présente SEULEMENT quand un credential existe :
    son absence veut dire « aucune clé ni compte ne résout pour toi ici » (ou
    `secret_kind=none`, rien à apporter), jamais « non calculé » — ça, l'enveloppe le
    dit (`credentials`)."""
    status: Literal["connected", "pending_step"]
    # Le palier de la cascade qui RÉPOND — la clé, pas le compte : un LinkedIn lié sur
    # la clé Unipile de l'org est `level=org`, `nature=hosted_account`.
    level: Literal["user", "group", "org", "tenant", "platform"]
    # api_key|basic_auth|fields|oauth|cookie|… (`secret_kind`), ou `hosted_account`
    # pour un canal hébergé. Jamais une valeur : la NATURE, sans secret.
    nature: Optional[str] = None
    # `connected` : le geste qui VÉRIFIE qu'il vit (outil de statut, sonde) ;
    # `pending_step` : le geste qui manque. Rendu tel quel, jamais reformulé.
    next_step: str


def etape_de_verification(connector: str) -> str:
    """Le geste qui VÉRIFIE qu'un credential disponible est vivant — rendu tel quel.

    D'abord le geste que le connecteur DÉCLARE (`status_hints.register_verify_step` :
    LinkedIn a son outil de statut), sinon la sonde générique sans effet de bord
    (`oto_instance op=verify`), sinon l'aveu qu'aucune n'existe. Jamais un silence :
    une ligne « connecté » sans geste se relit « vérifié »."""
    declare = status_hints.verify_step(connector)
    if declare:
        return declare
    if connector_verify.supports(connector):
        return (f"Disponible, pas encore vérifié vivant : "
                f"`oto_instance(op='verify', connector='{connector}')` le teste sans effet "
                f"de bord — à faire avant de conclure qu'il ne marche pas.")
    return ("Disponible, pas encore vérifié vivant : ce connecteur n'a pas de sonde "
            "sans effet de bord, seul un premier appel réel le dit.")


def _nature(name: str) -> Optional[str]:
    con = providers.REGISTRY.get(name)
    if con is None:
        return None
    return HOSTED_ACCOUNT if con.hosted_channel else con.secret_kind


def par_connecteur(sub: str, *, org: Optional[int], group: Optional[int]) -> dict[str, dict]:
    """`{connecteur: {status, level, nature, next_step}}` pour chaque connecteur dont
    un credential est disponible pour `sub` dans `(org, group)` — ABSENT sinon (aucune
    clé ni compte ne résout, ou le connecteur n'en demande pas : `secret_kind=none`).

    `org`/`group` EXPLICITES, comme `readiness.diagnose` : le calcul suit le sujet,
    jamais le contexte d'un requérant.

    Lève si le snapshot ne se lit pas : c'est à l'appelant de le DIRE (fail-visible),
    pas à ce module de rendre `{}` — un vide se relirait « rien n'est connecté », et
    c'est exactement la conclusion fausse qu'on répare."""
    snapshot = access.status_for(sub, org=org, group=group)["providers"]
    out: dict[str, dict] = {}
    for name, entry in snapshot.items():
        palier = _PALIER.get(entry.get("mode") or "")
        if palier is None:          # `forbidden` : rien ne résout
            continue
        attente = entry.get("pending_action")
        out[name] = {
            "status": PENDING_STEP if attente else CONNECTED,
            "level": palier,
            "nature": _nature(name),
            "next_step": attente or etape_de_verification(name),
        }
    return out


# Ce que l'enveloppe d'une surface dit du calcul — toujours, jamais un silence.
COMPUTED = "computed"
UNAVAILABLE = "unavailable"


def lire(sub: str, *, org: Optional[int]) -> tuple[dict[str, dict], str]:
    """`(par_connecteur(...), "computed")`, ou `({}, "unavailable")` si le snapshot ne
    se lit pas — journalisé, et RENDU : chaque surface pose ce statut dans son
    enveloppe, pour qu'une ligne sans `credential` ne se relise pas « rien n'est
    connecté » le jour où c'est la lecture qui a échoué. Le point d'entrée des deux
    surfaces agent (`oto_connector`, `oto_list_my_tools`) : un seul calcul, un seul
    échec possible, dit de la même façon.

    Pour l'APPELANT (les deux surfaces lisent sa propre toolbox) : l'équipe est son
    équipe active, lue DANS le `try` — un hoquet de lecture d'équipe est un échec du
    calcul comme un autre, dit de la même façon."""
    try:
        return par_connecteur(sub, org=org, group=access.current_group(sub)), COMPUTED
    except Exception:
        logger.warning("credential disponible illisible pour le catalogue (fail-visible)",
                       exc_info=True)
        return {}, UNAVAILABLE

"""Domicile unique de l'INSTRUCTION DE DÉPART servie à un agent hébergé.

⚠️ Tranché le 02/09/2026, redit le 03 : **le worker ne compose pas**. Il est un
client MCP qui exécute une instruction — il ne sait pas ce qu'elle contient, et
n'a rien à y ajouter. Une instruction par défaut vivant dans le runner
(`DEFAULT_INPUT`, retiré) faisait l'inverse : elle inventait le travail à la
place de qui l'avait déclaré, et le faisait dans le seul étage qui ne connaît
pas le métier — donc sans jamais pouvoir être ni relue, ni corrigée, ni même vue
depuis le produit.

Composer revient ICI, côté backend, qui sait ce qu'est une procédure et ce
qu'est une file. Et ce qu'on compose reste MINIME : une instruction rédigée à la
main est un **second domicile du métier** — la même règle vit dans la procédure
ET dans l'instruction, et l'une des deux finit par mentir. Une instruction qui
POINTE l'objet ne peut pas diverger de lui.

Les deux surfaces qui déclarent un agent (déclencheur, flotte) passent par ici :
une seule d'entre elles qui rédigerait sa propre variante rouvrirait le second
domicile à l'échelle du dépôt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

_BILAN = ("conclus par un bilan bref de ce que tu as fait et de ce qui t'a "
          "manqué.")


_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def horodatee(instruction: str, echeance: Union[datetime, str], tz: str) -> str:
    """L'instruction d'une occurrence PLANIFIÉE, plus le moment qu'elle représente.

    ⚠️ Un agent hébergé n'a pas d'horloge. Le 01/10/2026, le premier passage d'un
    agent quotidien a conclu `blocked` : il ne pouvait pas savoir quel jour on
    était, donc quelle veille couvrir. Le moment n'est pas du métier — c'est un
    FAIT de l'occurrence, que seule la plateforme connaît : l'échéance, et le
    fuseau dans lequel le déclencheur a été posé.

    L'échéance, et non l'heure de réservation : une veille du matin réservée
    quelques minutes plus tard couvre toujours le jour de son échéance.

    L'instruction écrite reste en tête, intacte ; le moment la suit, sur sa ligne.
    `echeance` naïve = UTC (la base se lit en `TimeZone=UTC`, cf. `db._conn`). Un
    fuseau inconnu LÈVE : la pose le valide (`validate_cron`), un fuseau devenu
    illisible est un défaut à voir, pas une heure à deviner."""
    if isinstance(echeance, str):
        echeance = datetime.fromisoformat(echeance)
    if echeance.tzinfo is None:
        echeance = echeance.replace(tzinfo=timezone.utc)
    locale = echeance.astimezone(ZoneInfo(tz))
    utc = echeance.astimezone(timezone.utc)
    return (f"{instruction}\n\nCette exécution est l'occurrence du "
            f"{_JOURS[locale.weekday()]} {locale:%d/%m/%Y à %H:%M} ({tz}), "
            f"soit {utc:%Y-%m-%dT%H:%M:%SZ}.")


def derivee(slug: str) -> str:
    """L'instruction MINIMALE : « lis l'objet, applique-le »."""
    return (f"Lis la procédure `{slug}` et applique-la. Elle fait autorité : "
            f"n'invente rien qu'elle ne dise, et {_BILAN}")


def de_file(slug: str, namespace: Optional[str],
            row_filter: Optional[dict[str, Any]] = None) -> str:
    """La même, plus la MÉCANIQUE DE FILE — qui n'est pas du métier.

    « Réserve une ligne, une seule, rends-la » appartient à la flotte, pas à la
    procédure : c'est la plateforme qui distribue le travail entre plusieurs
    agents. L'écrire ici, et non dans la procédure, garde chaque règle chez qui
    la porte — et évite qu'un client recopie à la main, dans chaque campagne, un
    protocole que la plateforme est seule à savoir juste.

    Sans cible déclarée, il n'y a pas de file à décrire : on rend l'instruction
    nue plutôt qu'un protocole qui désignerait un tableau imaginaire.

    ⚠️ Le protocole nomme l'`_id` que la réservation a rendu, jamais un pronom. Il a dit
    « écris sur `@claimed` » jusqu'au 07/09/2026 : ce raccourci se résolvait par le run
    courant, et un agent sans état n'en a pas un stable — l'instruction enseignait donc
    un geste dont une part des exécutions ne pouvait pas aboutir.

    ⚠️ **La cible est l'IDENTIFIANT que la campagne garde** (#1067) : résolue une fois à
    la déclaration, dans la portée du déclarant. La nommer par son nom faisait réserver
    l'agent dans SA portée, donc chez un homonyme apparu depuis. La réservation rend le
    même `ns_id`, et le protocole dit d'employer ce numéro dans les appels d'après.
    """
    if not namespace:
        return derivee(slug)
    filtre = (f", filtre `{json.dumps(row_filter, ensure_ascii=False)}`"
              if row_filter else "")
    return (
        f"Lis la procédure `{slug}` et applique-la : elle fait autorité, "
        "n'invente rien qu'elle ne dise.\n"
        f"Ta file de travail est le tableau `{namespace}`{filtre}. Réserve UNE "
        "SEULE ligne avec `data_claim_next` — jamais une deuxième, un autre "
        "agent prendra la suivante. Si la réservation ne rend rien, la file est "
        "vide : conclus et arrête-toi.\n"
        "La réservation te rend `ns_id`, le NUMÉRO du tableau : adresse-le par ce "
        "numéro dans tous tes appels suivants (`namespace: 174`). Un nom marche "
        "encore, il est en cours de retrait.\n"
        "Écris ton résultat en un seul appel sur la ligne que tu as réservée : "
        "`data_claim_next` t'a rendu son `_id`, passe-le dans `id`. Relis-la, puis "
        f"{_BILAN}")

"""Le corps JSON d'une requête REST, ou un refus NOMMÉ. **Un seul seam.**

Le patron `try: body = await request.json() / except Exception: body = {}` était
recopié route par route, et il produisait un *succès déguisé* : un corps illisible
devenait « aucun champ demandé », donc les valeurs par DÉFAUT — le contraire de ce
que l'appelant avait écrit. Le cas le plus cher (inventaire du 2026-08-27, sites B2
et B3) : `POST /api/me/tokens {"scopes": …}` mal formé émettait un jeton API **NON
PORTÉ** — les droits pleins du sub — à la place du jeton borné demandé ; côté
super-admin, non porté **et sans expiration**.

La politique était déjà écrite ailleurs dans le même code : quatorze routes rendent
`400 invalid_json`. Ce module en fait la SEULE définition, et le commentaire de
`capabilities/_rest_adapter.py` qui l'exige mot pour mot (« REFUSER un champ inconnu,
jamais l'IGNORER ») cesse d'avoir un trou juste au-dessus de lui.

**Trois cas, trois réponses** — la distinction est le cœur du module :

| ce qui arrive | réponse | pourquoi |
|---|---|---|
| corps **absent** (0 octet) | `{}` | rien n'a été demandé : les défauts sont le contrat (`POST /api/me/tokens` sans corps = un jeton `cli` non porté, et c'est voulu) |
| corps **illisible** | `InvalidJsonBody("invalid_json")` | quelque chose a été demandé et n'a pas été compris — le deviner, c'est servir autre chose que ce qui a été écrit |
| corps **valide mais pas un objet** | `InvalidJsonBody("invalid_body")` | une liste ou un scalaire n'a pas de champs à fusionner ; l'ignorer rejoue le même défaut |

L'exception plutôt qu'un `(body, réponse)` : les appelants ne rendent pas leurs
erreurs de la même façon (`_json_error` avec CORS, `json_error` injecté dans
l'adaptateur de capacités, `JSONResponse` nu + en-têtes CORS dans les deux façades
OAuth). Le seam tient la POLITIQUE ; chacun garde son rendu.
"""
from __future__ import annotations

import json
import re

from starlette.requests import Request

# Les deux échappements JSON qui donnent une chaîne que PostgreSQL ne peut pas porter :
# `\u0000` (aucune colonne texte ni `jsonb` ne prend l'octet NUL) et une moitié de paire
# `\ud800`–`\udfff` seule (elle ne s'encode pas en UTF-8). Filtre sur les octets bruts,
# pour ne parcourir le corps décodé que lorsqu'il peut en contenir une.
_ECHAPPEMENT_SUSPECT = re.compile(rb"\\u(?:0000|[dD][89abcdefABCDEF])")


class InvalidJsonBody(Exception):
    """Corps de requête présent mais inexploitable. Porte le code d'erreur à rendre
    (`invalid_json` / `invalid_body`) et un détail actionnable."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def read_json_body(request: Request) -> dict:
    """Le corps JSON de `request` comme dict. Lève `InvalidJsonBody` s'il est présent
    et inexploitable ; rend `{}` s'il est absent (cf. le tableau du module)."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise InvalidJsonBody(
            "invalid_json",
            f"Corps de requête illisible : {e}. Attendu : un objet JSON "
            f"(ou aucun corps du tout pour n'utiliser que les valeurs par défaut).")
    if not isinstance(body, dict):
        raise InvalidJsonBody(
            "invalid_body",
            f"Corps de requête JSON valide mais de type `{type(body).__name__}` : "
            f"un objet est attendu.")
    if _ECHAPPEMENT_SUSPECT.search(raw):
        chemin = _texte_impossible(body)
        if chemin is not None:
            # Durcissement CASA 6.2.1 : la chaîne partait telle quelle au SQL et le
            # pilote levait en pleine écriture — un 500 sur la réservation, le
            # commentaire, l'écriture d'une ligne… Aucun usage légitime.
            raise InvalidJsonBody(
                "invalid_body",
                f"`{chemin}` contains a NUL character (\\u0000) or an unpaired "
                "surrogate: such text cannot be stored. Remove it and retry.")
    return body


def _texte_impossible(valeur) -> str | None:
    """Le chemin (`filters[0].value`, `<clé>` pour une clé) de la première chaîne du
    corps qui porte un NUL ou une moitié de paire de substitution seule ; `None` sinon.
    Itératif : la profondeur d'un corps est celle que `json.loads` a acceptée."""
    pile = [("", valeur)]
    while pile:
        chemin, v = pile.pop()
        if isinstance(v, str):
            if not _texte_stockable(v):
                return chemin or "(body)"
        elif isinstance(v, dict):
            for k, x in v.items():
                sous = f"{chemin}.{k}" if chemin else str(k)
                if not _texte_stockable(k):
                    return (f"{chemin}.<key>" if chemin else "<key>")
                pile.append((sous, x))
        elif isinstance(v, list):
            pile.extend((f"{chemin}[{i}]", x) for i, x in enumerate(v))
    return None


def _texte_stockable(s: str) -> bool:
    if "\x00" in s:
        return False
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True

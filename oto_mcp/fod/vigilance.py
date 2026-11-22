"""Client HTTP mince vers le service FOD — capacité « vigilance » (gels d'avoirs).

Le contrôle d'un nom contre le registre national des gels d'avoirs (DG Trésor), indexé
par FOD (`POST /api/vigilance/gels-avoirs/controle`). Le nom part dans le CORPS, jamais
dans le chemin ; FOD n'en conserve rien.

Un 503 de cette route n'est pas une saturation : il dit que le registre ne permet pas
de conclure (pas chargé, tronqué ou trop ancien). Il ne se reprend donc pas et se nomme
à l'appelant (`RegistreNonConcluant`), pour qu'aucun agent n'y lise « aucun gel ».
"""
from __future__ import annotations

from typing import Any, Optional

from .http import etat as _etat

_CHEMIN = "/api/vigilance/gels-avoirs/controle"


class RegistreNonConcluant(Exception):
    """Le registre des gels d'avoirs ne permet pas de conclure en ce moment."""


def gels_avoirs_controler(nom: str, *, prenom: Optional[str] = None, limit: int = 10) -> dict[str, Any]:
    corps: dict[str, Any] = {"nom": nom, "limit": limit}
    if prenom:
        corps["prenom"] = prenom
    r = _etat("POST", _CHEMIN, (200, 503), body=corps)
    if r.status_code == 503:
        raise RegistreNonConcluant(
            "Le registre des gels d'avoirs ne permet pas de conclure en ce moment (pas chargé, "
            "tronqué ou trop ancien). Ce n'est PAS une absence de correspondance : ne concluez "
            "pas que le nom n'est pas inscrit, réessayez plus tard.")
    return r.json()

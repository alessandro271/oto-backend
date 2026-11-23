"""Client HTTP mince vers le service FOD — BOFiP-Impôts.

Les commentaires fiscaux en vigueur de la DGFiP, servie par FOD en passe-plat vers le jeu
Opendatasoft `bofip-vigueur` (`GET <_CHEMIN>`, `<_CHEMIN>/{identifiant}`).
Une référence inconnue rend `null` : c'est l'outil qui le nomme, jamais un `{}` silencieux.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from .http import get as _get

# Le chemin que sert FOD, hors de ce dépôt (cf. PLAFONDS de tests/test_vocabulaire_guide.py).
_CHEMIN = "/api/doctrine/bofip"


def bofip_rechercher(query: str, *, limit: int = 6) -> dict[str, Any]:
    return _get(_CHEMIN, params={"query": query, "limit": limit})


def bofip_lire(identifiant: str, *, max_chars: int = 12_000) -> Optional[dict[str, Any]]:
    return _get(f"{_CHEMIN}/{quote(identifiant, safe='')}",
                params={"max_chars": max_chars})

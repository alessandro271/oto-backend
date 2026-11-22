"""Client HTTP mince vers le service FOD — capacité « doctrine » (BOFiP-Impôts).

La doctrine fiscale en vigueur de la DGFiP, servie par FOD en passe-plat vers le jeu
Opendatasoft `bofip-vigueur` (`GET /api/doctrine/bofip`, `/api/doctrine/bofip/{identifiant}`).
Une référence inconnue rend `null` : c'est l'outil qui le nomme, jamais un `{}` silencieux.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from .http import get as _get


def bofip_rechercher(query: str, *, limit: int = 6) -> dict[str, Any]:
    return _get("/api/doctrine/bofip", params={"query": query, "limit": limit})


def bofip_lire(identifiant: str, *, max_chars: int = 12_000) -> Optional[dict[str, Any]]:
    return _get(f"/api/doctrine/bofip/{quote(identifiant, safe='')}",
                params={"max_chars": max_chars})

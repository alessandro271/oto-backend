"""Marque oto — source unique du favicon servi côté backend.

La source de vérité du mark est `oto-studio/brand/` : le fichier recopié ici est
`brand/logos/oto/oto-dashboard-mark.svg`, l'« open O » saffran (anneau ouvert,
caps arrondis). On le duplique en inline pour que les pages et endpoints
auto-portés du backend (share_ui, page de doc publique, `/favicon.svg` et
`/favicon.ico` de l'endpoint MCP) affichent le mark sans requête réseau ni asset
à déployer. Toute évolution du mark part de `oto-studio/brand/` et se reporte
ici, à l'octet près.
"""
from __future__ import annotations

import base64

# Identique octet pour octet à `oto-studio/brand/logos/oto/oto-dashboard-mark.svg`.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-64 -64 128 128">'
    '<circle r="44" fill="none" stroke="#f0b41e" stroke-width="28"'
    ' stroke-linecap="round" stroke-dasharray="230 46" transform="rotate(-8)">'
    '</circle>'
    '</svg>'
)

# Balise <link> auto-portée (data-URI) pour les pages HTML server-side.
FAVICON_LINK = (
    '<link rel=icon type="image/svg+xml" href="data:image/svg+xml;base64,'
    + base64.b64encode(FAVICON_SVG.encode("utf-8")).decode("ascii")
    + '">'
)

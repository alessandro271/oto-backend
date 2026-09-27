"""`UneFicheParLigneMiddleware` — une grosse liste se sert UNE FICHE PAR LIGNE.

Le canal texte sert le JSON sur une seule ligne. Tant qu'il reste dans le contexte du
modèle, c'est sans conséquence. Mais un client comme Claude Code RANGE un gros résultat
dans un fichier et ne le fait relire que par morceaux de LIGNES (`Read` avec
`offset`/`limit`, plafonné à 25 000 jetons par lecture) : un résultat d'une seule ligne
y est alors illisible, en entier comme par morceaux. Mesuré le 27/09/2026 sur le CLI
2.1.281 : une page rangée en une ligne de 61 350 jetons, refusée même avec `limit` ; la
même page, une fiche par ligne, lue à la ligne 900 sans difficulté.

Ce middleware réémet le canal texte d'un GROS résultat (≥ `SEUIL` caractères) dont une
liste de fiches porte l'essentiel : chaque fiche sur sa ligne. Le texte reste du JSON
VALIDE et identique une fois parsé — seuls des retours à la ligne s'ajoutent entre les
fiches — donc un client qui parse le texte ne voit aucune différence. Le canal
STRUCTURÉ ne bouge pas.

Ne s'applique qu'à :
- une liste de fiches (dicts) servie seule, ou
- un dict dont UNE clé porte une liste d'au moins deux fiches (la plus lourde, si
  plusieurs) — l'enveloppe d'une page : `{"total": …, "rows": [...], "next": …}`.
Sous le seuil, rien ne change : un petit résultat reste dans le contexte, et chaque
retour à la ligne y coûterait un jeton pour rien.

**Place dans la chaîne** : juste sous `MarkdownBody`, donc plus EXTERNE que tout ce qui
réémet le canal texte en JSON compact (l'écho de compte, la rédaction) — plus interne,
l'un d'eux rétablirait la ligne unique. Il lit le texte APRÈS la rédaction : un champ
rédigé ne revient pas.
"""
from __future__ import annotations

import json

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

# Sous ce seuil, le résultat reste dans le contexte du modèle : rien à relire.
SEUIL = 20_000


def _compact(valeur) -> str:
    return json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))


def _fiches(valeur) -> bool:
    return isinstance(valeur, list) and len(valeur) >= 2 and all(isinstance(x, dict) for x in valeur)


def _lignes(fiches: list) -> str:
    return "[\n" + ",\n".join(_compact(f) for f in fiches) + "\n]"


def rendu(texte: str) -> str | None:
    """Le texte une fiche par ligne, ou `None` s'il n'y a rien à faire."""
    if len(texte) < SEUIL:
        return None
    try:
        charge = json.loads(texte)
    except ValueError:
        return None
    if _fiches(charge):
        return _lignes(charge)
    if not isinstance(charge, dict):
        return None
    candidates = [c for c, v in charge.items() if _fiches(v)]
    if not candidates:
        return None
    cle = max(candidates, key=lambda c: len(_compact(charge[c])))
    parties = [f"{_compact(c)}:{_lignes(v) if c == cle else _compact(v)}" for c, v in charge.items()]
    return "{" + ",".join(parties) + "}"


class UneFicheParLigneMiddleware(Middleware):
    """Sert une grosse liste de fiches une fiche par ligne. Cf. le module."""

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if getattr(result, "is_error", False):
            return result
        contenu = getattr(result, "content", None) or []
        if len(contenu) != 1 or not isinstance(contenu[0], TextContent):
            return result
        texte = rendu(contenu[0].text)
        if texte is None:
            return result
        return ToolResult(
            content=[TextContent(type="text", text=texte)],
            structured_content=getattr(result, "structured_content", None),
            meta=getattr(result, "meta", None),
        )

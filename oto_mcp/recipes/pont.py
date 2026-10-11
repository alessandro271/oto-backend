"""Ce que le pont du bac à sable (`functions/pont.py`) a le droit d'appeler : les outils
DÉCLARÉS EN LECTURE, sous les mêmes gardes qu'`oto_call` (#1202, lot 1).

Le pont ne décide d'aucun droit ; cette fonction les porte tous, et ce sont ceux des
recettes déclaratives, au même endroit : `moteur._outil` (outil de connecteur, ni outil
de plateforme ni outil à modèle, déclaré en lecture — refus nommé sinon) puis
`moteur._appeler` → `meta.executer_cible` (activation du connecteur, axes d'appel, org du
run, journal sous le nom de l'outil, facturation, rédaction). L'identité est celle de
l'appelant (`sub`) : le code qui tourne n'en a aucune à lui.

Aucune écriture dans ce lot : un outil qui écrit est refusé ici, avant tout appel, comme
pour une recette. L'ouverture à une liste fermée d'outils d'écriture attend la règle de
publication (auteur ≠ appelant, #1202).
"""
from __future__ import annotations

from typing import Optional

from .. import redaction
from ..mcp_errors import McpError
from . import moteur


def appeleur_en_lecture(fastmcp, sub: Optional[str]):
    """L'`appeler` du pont pour `sub` : un outil en lecture, ou une réponse `ok: false`
    nommée. Ne lève pas — un refus est une réponse."""
    async def appeler(outil: str, arguments: dict) -> dict:
        try:
            tool = await moteur._outil(fastmcp, outil)
        except moteur.RecetteRefusee as e:
            return {"ok": False, "code": e.code, "message": str(e)}
        try:
            issue = await moteur._appeler(tool, sub, outil, dict(arguments))
        except McpError as e:
            # Arguments refusés par le schéma de l'outil, garde d'activation : rien n'a
            # été appelé.
            return {"ok": False, "code": "call_refused", "message": str(e.error.message)[:500]}
        if not issue.ok:
            return {"ok": False, "code": issue.code or "tool_failed",
                    "message": issue.message or "", "quantity": issue.quantity}
        if issue.retenu:
            return {"ok": False, "code": "redaction_withheld",
                    "message": "The org's redaction policy withheld this tool's output.",
                    "quantity": issue.quantity}
        return {"ok": True, "result": redaction.extract_payload(issue.result),
                "quantity": issue.quantity}
    return appeler

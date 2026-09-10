"""Outils MCP RETIRÉS — le nom ne répond plus : il REFUSE en nommant le geste qui aboutit.

Un retrait n'est pas un renommage (`deprecations.TOOLS`) : il n'y a pas de nom
d'aujourd'hui vers lequel traduire l'appel, donc rien à servir « à côté ». Ce n'est pas
non plus un nom simplement inconnu : `error_taxonomy._surviving_siblings` DÉRIVE la
fratrie d'un nom disparu, ce qui suffit quand ses verbes ont été rangés sous un voisin
(`gmail_search` → `gmail_message`). Pour un retrait, la fratrie serait les ~60 outils
`oto_*` — une liste qui n'apprend rien — alors que le geste qui aboutit est connu et
précis. Il s'écrit donc, une fois, ICI, et trois surfaces le lisent :

- `error_taxonomy.classify` — un `tools/call` direct sur le nom (fastmcp : « Unknown
  tool ») ;
- `tools/meta.oto_call` — AVANT le refus méta/spine : un nom `oto_*` y recevait « appelle-le
  directement », qui renvoie au chemin précédent. Une boucle ;
- `tools/meta.oto_tool_schema` — ce que l'agent lit avant d'appeler.

⚠️ **Le refus doit être AUTOSUFFISANT.** Il est lu par des agents qu'une procédure ou une
page d'org — contenu client, qu'on ne corrige pas — envoie encore vers ce nom : aucun
renvoi à un guide, aucune hypothèse sur ce que l'agent a lu avant.

Module PUR (aucun import `oto_mcp`) : la taxonomie d'erreurs doit rester importable seule.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class Retrait(NamedTuple):
    #: Ce que l'agent lit : ce qui a disparu, pourquoi rien n'est perdu, et le geste.
    message: str
    #: Le `hint` de l'enveloppe d'erreur (`data.oto.hint`) : le geste seul.
    hint: str


#: `oto_kb` — retiré le 10/09/2026 (décision d'Alexis) : la « base de connaissance » n'existe
#: plus comme concept, il n'y a que projet, doc et procédure. La DONNÉE ne bouge pas : c'était
#: déjà un projet ordinaire. La route REST `POST /api/me/kb` reste, le tableau de bord la
#: consomme (cf. `capabilities/kb.py`).
KB = Retrait(
    message=(
        "`oto_kb` is retired: there is no org \"knowledge base\" any more — oto has "
        "projects, docs and procedures, nothing else. What was called the knowledge base "
        "is an ordinary PROJECT of the org; nothing was moved or deleted, its pages are all "
        "still there. Reach them like any project's pages: find the project with "
        "`oto_project op=list` (seeded as \"Knowledge base\", or \"Base de connaissance\" in "
        "older orgs, and possibly renamed since), then read or write its pages with "
        "`oto_doc` and that `project_id` (`op=search`, `op=get`, `op=create`, "
        "`op=update`). Durable knowledge you capture goes, as a doc, into the project it "
        "belongs to. If a procedure or a page told you to call `oto_kb`, it predates this "
        "retirement: take the path above instead — this name only returns this refusal, "
        "called directly or through `oto_call`."),
    hint=("find the project with oto_project op=list, then read or write its pages with "
          "oto_doc and its project_id"),
)

RETIRES: dict[str, Retrait] = {"oto_kb": KB}


def retrait(nom: str) -> Optional[Retrait]:
    """Le refus servi pour un nom CANONIQUE retiré, ou None. L'appelant canonicalise
    d'abord (préfixe de tenant) : `acme_kb` d'un tenant arrive ici en `oto_kb`."""
    return RETIRES.get(nom)

"""Les recettes EN CODE (#1202, lot 2) : le corps d'une recette est du Python, joué dans
le bac à sable d'`oto_function` par le pont (`functions/pont.py`).

Une recette-code se range comme une recette déclarative — mêmes tables, même cycle
proposée → publiée, même `oto_recipe` — et s'en distingue par `kind: "code"` :

    {"kind": "code",
     "sources": {"recette.py": "async def run(oto, params): ..."},
     "entrypoint": "recette:run",
     "params": {"q": {"required": true}},
     "limits": {"max_units": 200, "max_calls": 100, "max_seconds": 120}}

Le code reçoit `oto` et les `params` résolus ; il appelle des outils (`await
oto.call(outil, arguments)`) et rend un reçu — des COMPTES, pas des données : il est
borné à `MAX_RECU` caractères. Ce qu'il peut appeler est jugé par `pont.appeleur_de_recette` :
outils de connecteur déclarés en lecture, `data_rows`, et `data_write` dans le seul
tableau désigné par `op=run` (jamais en épreuve).

**Qui publie** : une recette-code tourne avec les droits de qui l'EXÉCUTE, pas de qui
l'a écrite (auteur ≠ appelant, #1202). Tant que cette règle n'est pas tranchée, l'épreuve
et la publication d'une recette-code sont réservées à la PLATEFORME — la règle
d'`oto_function`. L'écrire et la proposer restent ouverts au membre.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from ..functions import contract as contrat_fonctions
from ..functions import pont as pont_bac
from . import contrat, pont

KIND = "code"
_CLES = {"kind", "sources", "entrypoint", "params", "limits"}
#: Le reçu que rend le code : des comptes. Au-delà, il est remplacé par un refus — le
#: reçu n'est pas un canal de données.
MAX_RECU = 4000
MAX_APPELS = 1000
MAX_SECONDES = 600


def est_code(corps: Any) -> bool:
    return isinstance(corps, dict) and corps.get("kind") == KIND


def valider(corps: Any) -> dict:
    """Le corps normalisé (défauts posés), ou `contrat.RecetteInvalide`."""
    probs: list[str] = []
    c = copy.deepcopy(corps)
    inconnues = sorted(set(c) - _CLES)
    if inconnues:
        probs.append(f"unknown keys for a code recipe: {inconnues} (allowed: {sorted(_CLES)})")
    try:
        contrat_fonctions.valider(c.get("sources"), c.get("entrypoint") or "", [])
    except contrat_fonctions.VersionInvalide as e:
        probs.append(str(e))
    params = c.setdefault("params", {})
    if not isinstance(params, dict):
        probs.append("`params` is an object {name: {required?, default?}}")
    lim = c.get("limits")
    if not isinstance(lim, dict) or not isinstance(lim.get("max_units"), (int, float)) \
            or isinstance(lim.get("max_units"), bool) \
            or not 0 < lim["max_units"] <= contrat.MAX_UNITS:
        probs.append(f"`limits.max_units` (a hard cap on what the tools BILL, 1 to "
                     f"{contrat.MAX_UNITS}) is required: there is no default")
    else:
        lim.setdefault("max_calls", 100)
        lim.setdefault("max_seconds", 120)
        if not isinstance(lim["max_calls"], int) or not 1 <= lim["max_calls"] <= MAX_APPELS:
            probs.append(f"`limits.max_calls`: 1 to {MAX_APPELS}")
        if not isinstance(lim["max_seconds"], int) \
                or not 10 <= lim["max_seconds"] <= MAX_SECONDES:
            probs.append(f"`limits.max_seconds`: 10 to {MAX_SECONDES}")
    if probs:
        raise contrat.RecetteInvalide(probs)
    return c


async def executer(corps: dict, params: dict, *, fastmcp, sub: Optional[str],
                   datastore: Any = None, ecrire: bool) -> dict:
    """Joue une recette-code VALIDÉE. `ecrire=False` = l'épreuve : aucune écriture,
    `data_write` refusé. Rend le reçu `{ok, result | error, calls, units, refused,
    stopped, done}` (+ `journal` en épreuve, où seule la plateforme lit)."""
    lim = corps["limits"]
    sortie = await pont_bac.executer_avec_pont(
        sources=corps["sources"], entrypoint=corps["entrypoint"], entree=params,
        appeler=pont.appeleur_de_recette(fastmcp, sub, datastore=datastore, ecrire=ecrire),
        budget=pont_bac.Budget(appels=lim["max_calls"], unites=float(lim["max_units"]),
                               delai_s=float(lim["max_seconds"])))
    recu = {k: sortie.get(k) for k in ("ok", "calls", "units", "refused", "stopped")}
    if sortie.get("ok"):
        if len(json.dumps(sortie.get("result"))) > MAX_RECU:
            recu.update(ok=False, error=f"receipt_too_large: the recipe returned more than "
                                        f"{MAX_RECU} characters — return counts, not data")
        else:
            recu["result"] = sortie.get("result")
    else:
        recu["error"] = sortie.get("error")
    recu["done"] = bool(recu["ok"]) and recu["stopped"] is None
    if not ecrire:
        recu["journal"] = sortie.get("journal") or ""
    return recu

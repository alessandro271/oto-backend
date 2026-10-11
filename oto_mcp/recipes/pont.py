"""Ce que le pont du bac à sable (`functions/pont.py`) a le droit d'appeler : les outils
DÉCLARÉS EN LECTURE, sous les mêmes gardes qu'`oto_call` (#1202, lot 1).

Le pont ne décide d'aucun droit ; cette fonction les porte tous, et ce sont ceux des
recettes déclaratives, au même endroit : `moteur._outil` (outil de connecteur, ni outil
de plateforme ni outil à modèle, déclaré en lecture — refus nommé sinon) puis
`moteur._appeler` → `meta.executer_cible` (activation du connecteur, axes d'appel, org du
run, journal sous le nom de l'outil, facturation, rédaction). L'identité est celle de
l'appelant (`sub`) : le code qui tourne n'en a aucune à lui.

Aucun outil de connecteur qui écrit : il est refusé ici, avant tout appel, comme pour une
recette déclarative. Une recette-code (`appeleur_de_recette`) écrit seulement dans le
tableau que son exécution désigne. L'ouverture à une liste fermée d'outils d'écriture
attend la règle de publication (auteur ≠ appelant, #1202).
"""
from __future__ import annotations

from typing import Any, Optional

from starlette.concurrency import run_in_threadpool

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


#: Ce qu'une page de `data_rows` peut rendre au code.
MAX_LIGNES_LUES = 500


def appeleur_de_recette(fastmcp, sub: Optional[str], *, datastore: Any, ecrire: bool):
    """L'`appeler` d'une recette-code (lot 2) : celui de la lecture, plus les tableaux.

    - `data_rows(datastore, filter?, fields?, limit?, cursor?)` : lire un tableau que
      l'appelant peut lire — une page à la fois (`MAX_LIGNES_LUES`).
    - `data_write(rows, key)` : écrire dans le SEUL tableau que `op=run` a désigné
      (`datastore`), avec les droits de l'appelant ; jamais en épreuve (`ecrire=False`).
      `key` est exigée : une relance repart du début, l'écriture doit être idempotente.

    Ces deux noms passent par la couche de stockage des outils `data_*`
    (`par_reference.tableau` puis le store), pas par un accès direct à la base. Tout
    autre nom suit `appeleur_en_lecture`."""
    lecture = appeleur_en_lecture(fastmcp, sub)

    async def appeler(outil: str, arguments: dict) -> dict:
        if outil == "data_rows":
            return await run_in_threadpool(_lire, arguments)
        if outil == "data_write":
            if not ecrire:
                return {"ok": False, "code": "write_in_test",
                        "message": "A test writes nothing: `data_write` only runs in "
                                   "`op=run`."}
            if datastore is None:
                return {"ok": False, "code": "no_target_table",
                        "message": "`data_write` writes into the table `op=run` names: "
                                   "pass `datastore` to the run."}
            return await run_in_threadpool(_ecrire, datastore, arguments)
        return await lecture(outil, arguments)
    return appeler


def _refus_tableau(e: Exception, code_valeur: str) -> dict:
    """Un refus du stockage, nommé ; `code_valeur` pour une valeur refusée
    (`ValueError`). Toute autre exception est re-levée : c'est un défaut, pas un refus."""
    from ..datastore import jetons
    from ..datastore.errors import DatastoreNotFound, DatastoreReadOnly
    if isinstance(e, DatastoreNotFound):
        return {"ok": False, "code": "datastore_not_found", "message": "Table not found."}
    if isinstance(e, DatastoreReadOnly):
        return {"ok": False, "code": "datastore_read_only",
                "message": "This table is shared with you read-only."}
    if isinstance(e, jetons.JetonMalPlace):
        return {"ok": False, "code": "invalid_datastore", "message": str(e)[:300]}
    if isinstance(e, ValueError):
        return {"ok": False, "code": code_valeur, "message": str(e)[:300]}
    raise e


def _lire(arguments: dict) -> dict:
    from ..datastore import par_reference as pr
    inconnus = sorted(set(arguments) - {"datastore", "filter", "fields", "limit", "cursor"})
    if inconnus or "datastore" not in arguments:
        return {"ok": False, "code": "invalid_call",
                "message": "`data_rows` takes datastore, filter?, fields?, limit?, cursor?"
                           + (f" (unknown: {inconnus})" if inconnus else "")}
    limite = arguments.get("limit", 100)
    if not isinstance(limite, int) or not 1 <= limite <= MAX_LIGNES_LUES:
        return {"ok": False, "code": "invalid_call",
                "message": f"`limit`: 1 to {MAX_LIGNES_LUES}"}
    try:
        store, adresse = pr.tableau(arguments["datastore"], ecrire=False)
        page = store.cursor_rows(adresse, filter=arguments.get("filter"), limit=limite,
                                 cursor=arguments.get("cursor"))
    except Exception as e:  # noqa: SILENT — refus nommé par `_refus_tableau`, re-levé sinon
        return _refus_tableau(e, "invalid_filter")
    lignes = page.get("rows") or []
    champs = arguments.get("fields")
    if isinstance(champs, list) and champs:
        lignes = [{k: v for k, v in r.items() if k in champs or k == "_id"} for r in lignes]
    return {"ok": True, "result": {"rows": lignes, "next_cursor": page.get("next_cursor")}}


def _ecrire(datastore: Any, arguments: dict) -> dict:
    from ..datastore import par_reference as pr
    inconnus = sorted(set(arguments) - {"rows", "key", "datastore"})
    lignes, cle = arguments.get("rows"), arguments.get("key")
    if inconnus or not isinstance(lignes, list) or not lignes \
            or not all(isinstance(r, dict) for r in lignes) \
            or not isinstance(cle, str) or not cle:
        return {"ok": False, "code": "invalid_call",
                "message": "`data_write` takes rows (a non-empty list of objects) and key "
                           "(the column that identifies a row: a re-run must not duplicate)"
                           + (f" (unknown: {inconnus})" if inconnus else "")}
    try:
        store, adresse = pr.tableau(datastore, ecrire=True)
        if "datastore" in arguments:
            _, autre = pr.tableau(arguments["datastore"], ecrire=False)
            if store._resolve(autre) != store._resolve(adresse):
                return {"ok": False, "code": "write_outside_target",
                        "message": "`data_write` only writes into the table `op=run` "
                                   "names."}
        bilan = store.write_rows(adresse, lignes, key=cle)
    except Exception as e:  # noqa: SILENT — refus nommé par `_refus_tableau`, re-levé sinon
        return _refus_tableau(e, "row_invalid")
    return {"ok": True, "result": {k: bilan.get(k) for k in ("inserted", "updated", "count")}}

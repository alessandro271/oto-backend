"""Ce qu'on remet au titulaire AVANT de purger son tenant, s'il le demande.

Le transfert vers son instance a eu lieu à un instant donné ; deux choses ont pu rester
chez nous seulement :

- le journal d'appels du périmètre (il voyage par tranches, et une partie a pu ne pas
  partir) : `journal` l'écrit en entier, un CSV gzip au format de `COPY`, comme les
  archives froides ;
- les lignes écrites APRÈS le transfert (`depuis`) : `tardives` écrit, table par table,
  les lignes du périmètre créées ou modifiées depuis, une ligne JSON par ligne de table
  (`row_to_json`, comme l'export).

⚠️ Ces fichiers portent du CONTENU : les produire est un geste sur décision, jamais un
contrôle de routine.
"""
from __future__ import annotations

import gzip
import json
import os

from .purge import Lecture

COLONNES_DE_DATE = ("created_at", "updated_at")


def journal(conn, lu: Lecture, chemin: str) -> int:
    if os.path.exists(chemin):
        raise FileExistsError(f"{chemin} existe déjà : une remise ne s'écrase pas")
    with gzip.open(chemin, "wb") as gz, conn.cursor().copy(
            f"COPY (SELECT * FROM tool_calls WHERE {lu.pred('tool_calls')} ORDER BY id) "
            "TO STDOUT WITH (FORMAT csv, HEADER true)", lu.params) as copie:
        for morceau in copie:
            gz.write(morceau)
    return int(conn.execute(f"SELECT count(*) AS n FROM tool_calls WHERE "
                            f"{lu.pred('tool_calls')}", lu.params).fetchone()["n"])


def tardives(conn, lu: Lecture, depuis: str, dossier: str) -> dict[str, int]:
    """Les lignes du périmètre créées ou modifiées après `depuis`, par table."""
    os.makedirs(dossier, exist_ok=True)
    out: dict[str, int] = {}
    for t in lu.ecrites:
        if t == "tool_calls":
            continue
        dates = [c for c in COLONNES_DE_DATE if c in lu.schema.colonnes.get(t, ())]
        if not dates:
            continue
        apres = " OR ".join(f"{c} > %(depuis)s" for c in dates)
        lignes = [r["l"] for r in conn.execute(
            f"SELECT row_to_json(x)::text AS l FROM {t} x WHERE ({lu.pred(t)}) AND ({apres})",
            {**lu.params, "depuis": depuis})]
        if not lignes:
            continue
        chemin = os.path.join(dossier, f"{t}.jsonl")
        if os.path.exists(chemin):
            raise FileExistsError(f"{chemin} existe déjà : une remise ne s'écrase pas")
        with open(chemin, "w", encoding="utf-8") as f:
            f.writelines(ligne + "\n" for ligne in lignes)
        out[t] = len(lignes)
    with open(os.path.join(dossier, "MANIFESTE.json"), "w", encoding="utf-8") as f:
        json.dump({"depuis": depuis, "tables": out}, f, indent=2)
    return out

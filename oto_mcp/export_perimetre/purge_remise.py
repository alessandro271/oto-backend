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
contrôle de routine. On ne remet JAMAIS un secret, même chiffré ou haché : les tables
de secrets (`TABLES_DE_SECRETS`) sont exclues, et de toute autre table tombent les
colonnes `secrets` du classement et toute colonne `*_enc` ou `*_hash`.
"""
from __future__ import annotations

import gzip
import json
import os

from .purge import Lecture

COLONNES_DE_DATE = ("created_at", "updated_at")
TABLES_DE_SECRETS = frozenset({"connector_credentials", "user_api_tokens",
                               "org_service_accounts"})


def colonnes_secretes(lu: Lecture, table: str) -> list[str]:
    return sorted(set(lu.classement[table].secrets)
                  | {c for c in lu.schema.colonnes.get(table, ())
                     if c.endswith("_enc") or c.endswith("_hash")})


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
        if t == "tool_calls" or t in TABLES_DE_SECRETS:
            continue
        dates = [c for c in COLONNES_DE_DATE if c in lu.schema.colonnes.get(t, ())]
        if not dates:
            continue
        apres = " OR ".join(f"{c} > %(depuis)s" for c in dates)
        lignes = [r["l"] for r in conn.execute(
            f"SELECT (to_jsonb(x) - %(secrets)s::text[])::text AS l FROM {t} x "
            f"WHERE ({lu.pred(t)}) AND ({apres})",
            {**lu.params, "depuis": depuis, "secrets": colonnes_secretes(lu, t)})]
        if not lignes:
            continue
        chemin = os.path.join(dossier, f"{t}.jsonl")
        if os.path.exists(chemin):
            raise FileExistsError(f"{chemin} existe déjà : une remise ne s'écrase pas")
        with open(chemin, "w", encoding="utf-8") as f:
            f.writelines(ligne + "\n" for ligne in lignes)
        out[t] = len(lignes)
    with open(os.path.join(dossier, "MANIFESTE.json"), "w", encoding="utf-8") as f:
        json.dump({"depuis": depuis, "tables": out,
                   "jamais_remis": sorted(TABLES_DE_SECRETS)}, f, indent=2)
    return out

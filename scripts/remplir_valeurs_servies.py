#!/usr/bin/env python3
"""Remplit le texte des valeurs servies des lignes nées AVANT sa matérialisation.

Les colonnes `valeurs_texte`/`valeurs_vec` de `datastore_rows` (`db/valeurs_servies.py`)
sont remplies par un déclencheur à chaque écriture de `data`. Les lignes écrites avant sa
pose restent vides : la recherche `q_scope=values` les trouve encore, par l'ancien
prédicat, mais à l'ancien prix — recalculer leurs valeurs servies. Ce script les remplit,
une fois.

⚠️ **La base est PARTAGÉE entre préproduction et production.** Lancé À LA MAIN, depuis le
commit qui l'apporte (ou un descendant), APRÈS la révision 0061 (le déclencheur doit
être posé : le script s'en sert, et sans lui une ligne écrite pendant le passage resterait
vide). **À blanc par défaut** : sans `--appliquer`, il dit ce qu'il reste à remplir, et
n'écrit rien.

    # 1. à blanc : ce qui reste, par tableau
    ./.venv/bin/python -m scripts.remplir_valeurs_servies [--tableau <ns_id> …]
    # 2. remplir, par lots
    ./.venv/bin/python -m scripts.remplir_valeurs_servies --appliquer [--lot 500] \\
        [--pause 0.05] [--tableau <ns_id> …]
    # 3. vérifier : un second passage à blanc doit annoncer 0

**Le geste : `UPDATE … SET data = data`** sur les lignes vides d'un lot. C'est le
déclencheur qui calcule, sur la ligne écrite (`NEW.data`) : une seule expression du texte
dans tout le dépôt, et jamais un texte calculé sur une lecture périmée — si la ligne a
changé entre la lecture du lot et l'écriture, PostgreSQL réévalue `valeurs_texte IS NULL`
sur la version courante, et l'écriture concurrente l'a déjà remplie. Le geste n'avance ni
la révision de la ligne ni son journal (tous deux conditionnés à un `data` DIFFÉRENT), ni
`updated_at` (écrit par le code, pas par la base).

**Par lots, dans l'ordre de la clé** (`ns_id`, `row_id`), chacun dans sa transaction :
jamais un verrou de table, et une ligne verrouillée par un autre écrivain est SAUTÉE
(`SKIP LOCKED`), pas attendue — le passage suivant la reprend. Idempotent : une ligne
remplie n'est plus visée. Rien d'autre n'est touché.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from oto_mcp.db import valeurs_servies as vs
from oto_mcp.db._conn import _connect_autocommit

#: Une lecture du parc reste bornée : la base est partagée avec la production. Le
#: décompte parcourt `datastore_rows` entière — d'où plus que les 15 s d'une lecture.
STATEMENT_TIMEOUT_DECOMPTE = "120s"
#: Un lot : au plus `--lot` lignes, chacune recalculée une fois.
STATEMENT_TIMEOUT_LOT = "60s"
LOT_DEFAUT = 500


class DeclencheurAbsent(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"le déclencheur {vs.NOM_DECLENCHEUR} est absent : rien ne remplirait les "
            "lignes. Le poser d'abord (révision 0061 : `oto-mcp migrer upgrade head`, ou "
            "le démarrage du code qui l'apporte), puis relancer.")


def _filtre(tableaux: Optional[list[int]]) -> tuple[str, list]:
    if not tableaux:
        return "", []
    return " AND ns_id = ANY(%s)", [list(tableaux)]


def restant(conn, tableaux: Optional[list[int]] = None) -> list[dict]:
    """Les tableaux qui ont des lignes vides, et combien — du plus chargé au moins."""
    filtre, params = _filtre(tableaux)
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_DECOMPTE}'")
    return [dict(r) for r in conn.execute(
        f"SELECT ns_id, count(*) AS vides FROM {vs.TABLE} "
        f"WHERE {vs.COLONNE_TEXTE} IS NULL{filtre} GROUP BY ns_id ORDER BY vides DESC",
        params).fetchall()]


def un_lot(conn, apres: tuple, taille: int,
           tableaux: Optional[list[int]] = None) -> tuple[Optional[tuple], int]:
    """Un lot : les `taille` lignes suivant `apres` dans l'ordre de la clé ; celles qui
    sont vides et libres sont remplies. Rend (la dernière clé lue, ou None à la fin ;
    le nombre de lignes remplies)."""
    filtre, params = _filtre(tableaux)
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_LOT}'")
    with conn.transaction():
        cles = conn.execute(
            f"SELECT ns_id, row_id FROM {vs.TABLE} WHERE (ns_id, row_id) > (%s, %s)"
            f"{filtre} ORDER BY ns_id, row_id LIMIT %s",
            [*apres, *params, taille]).fetchall()
        if not cles:
            return None, 0
        fait = conn.execute(
            f"UPDATE {vs.TABLE} d SET data = d.data FROM ("
            f"  SELECT ns_id, row_id FROM {vs.TABLE} "
            f"  WHERE (ns_id, row_id) IN (SELECT * FROM unnest(%s::bigint[], %s::text[])) "
            f"    AND {vs.COLONNE_TEXTE} IS NULL "
            f"  FOR UPDATE SKIP LOCKED) v "
            f"WHERE d.ns_id = v.ns_id AND d.row_id = v.row_id",
            [[c["ns_id"] for c in cles], [c["row_id"] for c in cles]]).rowcount or 0
    dernier = cles[-1]
    return (dernier["ns_id"], dernier["row_id"]), fait


def executer(*, appliquer: bool = False, tableaux: Optional[list[int]] = None,
             lot: int = LOT_DEFAUT, pause: float = 0.05, sortie=print) -> dict:
    """Le passage : à blanc par défaut. Rend le bilan."""
    if lot < 1:
        raise ValueError(f"--lot {lot} : un entier ≥ 1 est attendu.")
    with _connect_autocommit() as conn:
        if conn.execute(vs.SQL_DECLENCHEUR_PRESENT).fetchone() is None:
            raise DeclencheurAbsent()
        avant = restant(conn, tableaux)
        total = sum(r["vides"] for r in avant)
        sortie(f"{'À BLANC — ' if not appliquer else ''}{total} ligne(s) vide(s) dans "
               f"{len(avant)} tableau(x)")
        for r in avant[:20]:
            sortie(f"  tableau {r['ns_id']} : {r['vides']}")
        if len(avant) > 20:
            sortie(f"  … et {len(avant) - 20} autre(s)")
        bilan = {"vides_avant": total, "remplies": 0, "lots": 0, "vides_apres": total}
        if not appliquer or not total:
            return bilan
        apres: Optional[tuple] = (-1, "")
        debut = time.monotonic()
        while apres is not None:
            apres, fait = un_lot(conn, apres, lot, tableaux)
            bilan["lots"] += 1
            bilan["remplies"] += fait
            if bilan["lots"] % 100 == 0:
                sortie(f"  … {bilan['remplies']} remplie(s), {bilan['lots']} lot(s), "
                       f"{time.monotonic() - debut:.0f} s")
            if pause and apres is not None:
                time.sleep(pause)
        bilan["vides_apres"] = sum(r["vides"] for r in restant(conn, tableaux))
        sortie(f"{bilan['remplies']} ligne(s) remplie(s) en {bilan['lots']} lot(s), "
               f"{time.monotonic() - debut:.0f} s ; restent {bilan['vides_apres']} "
               "(verrouillées pendant le passage, ou écrites sans le déclencheur : "
               "relancer)." if bilan["vides_apres"] else
               f"{bilan['remplies']} ligne(s) remplie(s) en {bilan['lots']} lot(s), "
               f"{time.monotonic() - debut:.0f} s ; plus aucune ligne vide.")
        return bilan


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--appliquer", action="store_true",
                   help="remplir (sans lui : à blanc, rien n'est écrit)")
    p.add_argument("--tableau", type=int, action="append",
                   help="limiter à ce tableau (ns_id) ; répétable")
    p.add_argument("--lot", type=int, default=LOT_DEFAUT,
                   help=f"lignes lues par lot (défaut {LOT_DEFAUT})")
    p.add_argument("--pause", type=float, default=0.05,
                   help="secondes entre deux lots (défaut 0.05)")
    a = p.parse_args(argv)
    bilan = executer(appliquer=a.appliquer, tableaux=a.tableau, lot=a.lot, pause=a.pause)
    return 0 if (not a.appliquer or bilan["vides_apres"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

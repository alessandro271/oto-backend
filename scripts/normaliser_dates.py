"""Normalise EN PLACE les dates déjà stockées dans les colonnes `date`/`datetime`
(oto-backend#859).

**Pourquoi.** Depuis #859, l'écriture lit une date sous toutes ses formes et en stocke
une seule (`oto_mcp/datastore/dates.py`). L'existant, lui, porte encore ce que les
agents ont écrit avant : mesuré le 30/09/2026 sur 225 tableaux, ~42 000 valeurs, dont
603 sans fuseau ou en date nue dans une colonne `datetime`, 1 456 avec un offset dans
une colonne `date`, et 1 003 qui ne sont pas du texte. Le tri et les filtres les lisent
déjà comme des instants ; ce script les ramène à la forme que l'écriture pose.

**Le juge est `dates.lire`**, le même que l'écriture : une valeur se réécrit en
`dates.lire(v).forme` — `AAAA-MM-JJTHH:MM:SSZ` pour un instant (un instant sans fuseau
est lu en UTC, et compté à part), `AAAA-MM-JJ` dans une colonne `date` (la date que
l'instant porte dans son propre fuseau), une date imprécise à sa précision.

**Ce qui n'est PAS touché, et se liste** : une valeur ILLISIBLE (texte libre, date
impossible) et une valeur qui n'est PAS une chaîne (nombre, booléen, objet) — même un
nombre qui se lirait comme un horodatage : l'arbitrage est de les montrer, pas de les
convertir. Le vide et les mots réservés (`@empty`) ne sont pas des dates.

**Couverture** : toute case qu'une déclaration atteint — colonne de premier niveau,
sous-champ d'objet (`col.sous`), élément de liste (`col[]`, `col[].sous`), valeur
déballée de ses couches (`{"valeur": …}`, la couche `origine` n'étant jamais réécrite).

**Une clé métier de type date** est signalée par tableau : la réécrire change la valeur
que l'index d'unicité compare au caractère près. Si deux lignes portent la même date
sous deux formes, la seconde réécriture violerait l'index : elle est sautée (point de
sauvegarde par ligne), listée, et le reste du lot s'écrit.

## Lancement — une fois, par « oto cd », depuis le commit

    lanceur --script scripts/normaliser_dates.py                  # à blanc (défaut)
    lanceur --script scripts/normaliser_dates.py --appliquer
    lanceur --script scripts/normaliser_dates.py --appliquer --depuis 812:r-00042

La base est partagée entre préproduction et production : UNE exécution suffit. Un
second passage à blanc après `--appliquer` ne doit plus rien trouver à reprendre.

## Lots, délais, journal

- Tableau par tableau (ceux dont le schéma déclare un type `date`/`datetime`), par
  lots de `--taille-lot` lignes (défaut 2 000) bornés par `row_id`. Chaque lot est SA
  transaction, sous `statement_timeout` 15 s et `lock_timeout` 5 s.
- `--appliquer` : le lot relit ses lignes SOUS VERROU (`FOR UPDATE`) et les écrit dans
  la même transaction. À blanc : la même lecture, sans verrou ni écriture.
- **Journal des révisions** : l'écriture passe par `db.estampille.ecriture_de_lignes`,
  sous le geste interne `service:normalisation-dates` (source `system`, UN geste pour
  toute la reprise) : chaque ligne reprise laisse sa révision, l'état d'avant lisible
  par `data_row_history` le temps de la rétention du journal.
- Idempotent : une case reprise est dans sa forme, une relance ne la trouve plus.

Sorties : 0 = fait (ou à blanc) ; 2 = paramètres ; 4 = un lot a échoué (les lots
précédents sont validés en `--appliquer`, la ligne de reprise est imprimée).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Any, Optional

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from oto_mcp import geste
from oto_mcp.datastore import dates
from oto_mcp.datastore.declaration import champ_declare
from oto_mcp.db._conn import _connect
from oto_mcp.db.estampille import ecriture_de_lignes

#: L'acteur des révisions écrites par la reprise : `service:normalisation-dates`.
SERVICE = "normalisation-dates"
TAILLE_LOT = 2_000
_TIMEOUTS = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")
#: Préfiltre : les tableaux dont le schéma NOMME un type date. Le parcours déclaré
#: (`dates.parcourir_ligne`) reste le juge ; ce motif ne fait qu'éviter de lire les autres.
_TABLEAUX = ("SELECT id, schema FROM user_datastores "
             "WHERE schema IS NOT NULL AND id >= %(ns)s "
             "AND schema::text ~ '\"type\": *\"date(time)?\"' ORDER BY id")
_LOT = ("SELECT row_id, data FROM datastore_rows WHERE ns_id = %(ns)s "
        "AND row_id > %(rid)s ORDER BY row_id LIMIT %(n)s")
_ECRIRE = ("UPDATE datastore_rows SET data = %(data)s, updated_at = NOW() "
           "WHERE ns_id = %(ns)s AND row_id = %(rid)s")

#: Les catégories d'une case de date existante.
REPRISE, SANS_FUSEAU, ILLISIBLE, NON_CHAINE = (
    "reprise", "dont_sans_fuseau", "illisible", "non_chaine")


def reprendre_ligne(schema: Optional[dict], data: Any) -> tuple[Any, list]:
    """`(data reprise, constats)`. Pur. Un constat = `(catégorie, chemin, valeur)`.

    Seule une CHAÎNE lisible hors de sa forme est réécrite ; l'illisible et la
    non-chaîne restent telles quelles et sont constatées."""
    constats: list = []

    def _case(valeur: Any, ftype: str, chemin: str) -> Any:
        if valeur is None or valeur == "":
            return valeur
        if not isinstance(valeur, str):
            constats.append((NON_CHAINE, chemin, valeur))
            return valeur
        lue = dates.lire(valeur, ftype)
        if lue is None:
            if not valeur.startswith("@"):
                constats.append((ILLISIBLE, chemin, valeur))
            return valeur
        if lue.forme == valeur:
            return valeur
        constats.append((REPRISE, chemin, valeur))
        if lue.suppose_utc:
            constats.append((SANS_FUSEAU, chemin, valeur))
        return lue.forme

    return dates.parcourir_ligne(schema, data, _case), constats


def cle_de_type_date(schema: Optional[dict]) -> Optional[str]:
    """La clé métier déclarée, si c'est une colonne `date`/`datetime`."""
    cle = (schema or {}).get("key")
    if isinstance(cle, str) and (champ_declare(schema, cle) or {}).get("type") \
            in dates.TYPES_DATES:
        return cle
    return None


class LotEnEchec(RuntimeError):
    def __init__(self, depuis: tuple[int, str], cause: BaseException):
        self.depuis = depuis
        super().__init__(f"lot après {depuis[0]}:{depuis[1]} en échec "
                         f"({type(cause).__name__}: {cause})")


def _lot(conn, ns_id: int, schema: dict, apres: str, taille: int, *,
         appliquer: bool) -> dict:
    for s in _TIMEOUTS:
        conn.execute(s)
    sql = _LOT + (" FOR UPDATE" if appliquer else "")
    lignes = conn.execute(sql, {"ns": ns_id, "rid": apres, "n": taille}).fetchall()
    ecrites, constats, collisions = [], [], []
    for r in lignes:
        neuf, faits = reprendre_ligne(schema, r["data"])
        constats.extend((r["row_id"], *f) for f in faits)
        if neuf is r["data"]:
            continue
        if appliquer:
            try:
                with conn.transaction():          # point de sauvegarde par ligne
                    conn.execute(_ECRIRE, {"data": Jsonb(neuf), "ns": ns_id,
                                           "rid": r["row_id"]})
            except UniqueViolation:
                collisions.append(r["row_id"])
                continue
        ecrites.append(r["row_id"])
    fin = lignes[-1]["row_id"] if len(lignes) == taille else None
    return {"ecrites": ecrites, "constats": constats, "collisions": collisions,
            "fin": fin}


def _court(valeur: Any) -> str:
    texte = repr(valeur)
    return texte if len(texte) <= 80 else texte[:77] + "…"


def executer(*, appliquer: bool, taille: int = TAILLE_LOT,
             depuis: tuple[int, str] = (0, ""), dire=print) -> dict:
    """Parcourt les tableaux à dates, par lots. Lève `LotEnEchec` (qui nomme la
    reprise) au premier lot qui échoue ; en `appliquer`, les lots d'avant restent
    validés. Rend le bilan : `par_colonne[(ns_id, chemin)][catégorie]`, les lignes
    écrites, les collisions de clé métier et les tableaux à clé de type date."""
    if taille < 1:
        raise ValueError(f"--taille-lot : au moins 1, reçu {taille}")
    bilan: dict = {"tableaux": 0, "lots": 0, "lignes": 0, "collisions": [],
                   "cles_date": {}, "par_colonne": {}}
    with _connect() as conn:
        tableaux = conn.execute(_TABLEAUX, {"ns": depuis[0]}).fetchall()
    with geste.interne(SERVICE):
        for t in tableaux:
            ns_id, schema = t["id"], t["schema"]
            bilan["tableaux"] += 1
            cle = cle_de_type_date(schema)
            if cle:
                bilan["cles_date"][ns_id] = cle
                dire(f"  ⚠️ tableau {ns_id} : clé métier `{cle}` de type date — une "
                     f"collision de forme sera sautée et listée")
            apres = depuis[1] if ns_id == depuis[0] else ""
            while True:
                try:
                    if appliquer:
                        with ecriture_de_lignes() as conn:
                            lot = _lot(conn, ns_id, schema, apres, taille,
                                       appliquer=True)
                    else:
                        with _connect() as conn:
                            lot = _lot(conn, ns_id, schema, apres, taille,
                                       appliquer=False)
                            conn.rollback()
                except Exception as exc:
                    raise LotEnEchec((ns_id, apres), exc) from exc
                bilan["lots"] += 1
                bilan["lignes"] += len(lot["ecrites"])
                for row_id in lot["collisions"]:
                    bilan["collisions"].append((ns_id, row_id))
                    dire(f"  COLLISION de clé métier, non écrite : tableau {ns_id} "
                         f"ligne {row_id}")
                for row_id, cat, chemin, valeur in lot["constats"]:
                    compte = bilan["par_colonne"].setdefault((ns_id, chemin), Counter())
                    compte[cat] += 1
                    if cat in (ILLISIBLE, NON_CHAINE):
                        dire(f"  {cat.upper().replace('_', '-')}, non touchée : tableau "
                             f"{ns_id} ligne {row_id} `{chemin}` = {_court(valeur)}")
                if lot["fin"] is None:
                    break
                apres = lot["fin"]
    return bilan


def _depuis(texte: str) -> Optional[tuple[int, str]]:
    ns, sep, rid = texte.partition(":")
    return (int(ns), rid) if sep and ns.isdigit() else None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="scripts/normaliser_dates.py",
                                description=__doc__.splitlines()[0])
    p.add_argument("--appliquer", action="store_true", help="écrit (sinon à blanc)")
    p.add_argument("--taille-lot", type=int, default=TAILLE_LOT)
    p.add_argument("--depuis", help="<ns_id>:<row_id> — reprendre APRÈS cette clé")
    a = p.parse_args(argv)
    depuis = (0, "")
    if a.depuis:
        depuis = _depuis(a.depuis)
        if depuis is None:
            print(f"--depuis attend <ns_id>:<row_id>, reçu {a.depuis!r}", file=sys.stderr)
            return 2
    print("ÉCRITURE (--appliquer)" if a.appliquer else "À BLANC — rien ne sera écrit")
    try:
        bilan = executer(appliquer=a.appliquer, taille=a.taille_lot, depuis=depuis)
    except ValueError as refus:
        print(str(refus), file=sys.stderr)
        return 2
    except LotEnEchec as echec:
        print(f"\nÉCHEC : {echec}", file=sys.stderr)
        if a.appliquer:
            print("les lots précédents sont VALIDÉS ; reprendre par :\n  lanceur --script "
                  "scripts/normaliser_dates.py --appliquer --depuis "
                  f"{echec.depuis[0]}:{echec.depuis[1]}", file=sys.stderr)
        return 4
    total: Counter = Counter()
    print(f"\n{bilan['tableaux']} tableau(x) à dates, {bilan['lots']} lot(s) ; "
          f"{bilan['lignes']} ligne(s) {'reprises' if a.appliquer else 'à reprendre'}")
    for (ns_id, chemin), compte in sorted(bilan["par_colonne"].items()):
        total.update(compte)
        print(f"  tableau {ns_id} `{chemin}` : "
              + ", ".join(f"{k} {v}" for k, v in sorted(compte.items())))
    print("total : " + (", ".join(f"{k} {v}" for k, v in sorted(total.items()))
                        or "rien"))
    if bilan["cles_date"]:
        print(f"{len(bilan['cles_date'])} tableau(x) à clé métier de type date ; "
              f"{len(bilan['collisions'])} collision(s) sautée(s)")
    if not a.appliquer:
        print("passe à blanc — rien n'est écrit (--appliquer pour écrire)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

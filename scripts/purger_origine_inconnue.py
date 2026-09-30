"""Retire le marqueur « (origine inconnue) » des couches `origine` où il tient lieu de
valeur — otomata-tech/oto#116.

**Le défaut.** Le balayage qui accompagnait la déclaration tardive du cran
`origine: "system"` (oto#46, oto#70) écrivait `<champ>.origine = "(origine inconnue)"`
sur les lignes déjà là : une phrase pour l'œil humain, logée à l'emplacement exact d'une
valeur. Un écran de restitution l'a comparée à la valeur courante, les a trouvées
différentes, et en a conclu qu'une valeur fournie par un tiers avait été corrigée. Tout
consommateur qui ne connaît pas la chaîne exacte commet la même erreur. Le cran est
supprimé depuis le 08/09/2026, le balayage l'est depuis oto#116 ; restent les cases
qu'il a marquées (129 lignes, 3 tableaux, au constat du 10/09/2026).

**Le geste, par case marquée.** La couche `origine` part ; tout le reste de la case
reste à l'identique. Une case réduite à sa seule `valeur` redevient plate (sa forme
d'avant le balayage, qui l'avait enveloppée), sauf si cette valeur est un objet — la
déballer le ferait lire comme des couches. Une case qui ne portait QUE le marqueur (sa
valeur a été effacée depuis) devient `null`, ce que rend la fusion à l'effacement d'une
case sans origine (`columns._merge_column`). **L'absence d'origine se dit alors par
l'absence de la couche**, jamais par un texte à sa place.

**Ce qui n'est PAS repris, et se dit.** Une ligne dont le texte contient le marqueur
ailleurs qu'en couche `origine` de premier niveau (dans une valeur, un commentaire, une
origine en version objet, un élément de liste) est listée « hors motif » et jamais
touchée : ce n'est pas l'œuvre du balayage, et rien ne permet de la réécrire sans
deviner.

**Le juge est `sans_le_marqueur`**, en Python : la requête ne fait que PRÉFILTRER les
lignes dont le texte contient le marqueur. `CONSTAT_SQL` en est la version en lecture
seule (`--constat` l'imprime, précédée de son `statement_timeout` 15 s, pour psql) : son
compte `lignes_a_reprendre` doit égaler celui de la passe à blanc. Elle lit toute la
table d'un bloc ; si elle expire, la passe à blanc, découpée en lots, EST le constat.

## Lots, délais, journal

- **Par lots bornés par la clé primaire** `(ns_id, row_id)` — `--taille-lot` lignes
  (défaut 2 000), jamais un balayage d'un bloc : la borne haute d'un lot se lit par un
  parcours d'index, puis le lot ne lit que sa plage. Chaque lot est SA transaction, sous
  `statement_timeout` 15 s et `lock_timeout` 5 s.
- **`--apply`** : chaque lot relit ses lignes SOUS VERROU (`FOR UPDATE`) et les écrit
  dans la même transaction, validée à sa fin — un lot suivant qui échoue ne le défait
  pas. **À blanc** (défaut) : la même lecture, sans verrou ni écriture.
- **Journal des révisions** : l'écriture passe par `db.estampille.ecriture_de_lignes`,
  sous le geste interne `service:reprise-origine-inconnue` (source `system`, UN geste
  pour toute la reprise). Chaque case reprise laisse donc sa révision, avec l'origine
  retirée dans `avant` — l'état antérieur reste lisible (`data_row_history`) le temps de
  la rétention du journal. `rev` avance par son déclencheur, `updated_at` est avancé ici.
- **Idempotent** : une case reprise ne porte plus le marqueur, une relance ne la trouve
  plus. La reprise après un échec est la relance, ou `--depuis <ns_id>:<row_id>` (la
  ligne imprimée à l'échec) pour sauter ce qui a déjà été parcouru.

    lanceur --script scripts/purger_origine_inconnue.py            # à blanc
    lanceur --script scripts/purger_origine_inconnue.py --apply
    lanceur --script scripts/purger_origine_inconnue.py --apply --depuis 812:r-00042

La base est partagée entre préproduction et production : UNE exécution suffit.

Sorties : 0 = fait (ou passe à blanc) ; 2 = paramètres ; 4 = un lot a échoué (les lots
précédents sont validés en `--apply`, la ligne de reprise est imprimée).
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from psycopg.types.json import Jsonb

from oto_mcp import geste
from oto_mcp.datastore.couches import ORIGIN_LAYER, VALUE_LAYER, same_value
from oto_mcp.db._conn import _connect
from oto_mcp.db.estampille import ecriture_de_lignes

#: Le texte que le balayage retiré posait. Il ne vit plus qu'ici : aucun code du
#: serveur ne l'écrit ni ne le lit (garde : `tests/datastore/test_origine_inconnue_116.py`).
MARQUEUR = "(origine inconnue)"
#: L'acteur des révisions écrites par la reprise : `service:reprise-origine-inconnue`.
SERVICE = "reprise-origine-inconnue"
TAILLE_LOT = 2_000
_TIMEOUTS = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")

#: Le constat, en LECTURE SEULE, sur toute la base. `lignes_a_reprendre` = la forme que
#: reprend le script ; `hors_motif` = le texte ailleurs, qu'il ne touche pas.
CONSTAT_SQL = """\
SELECT ns_id,
       count(*) FILTER (WHERE porte)     AS lignes_a_reprendre,
       count(*) FILTER (WHERE NOT porte) AS hors_motif
  FROM (SELECT r.ns_id, EXISTS (
               SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(r.data) = 'object'
                                             THEN r.data ELSE '{}'::jsonb END) e
                WHERE jsonb_typeof(e.value) = 'object'
                  AND e.value -> 'origine' = to_jsonb('(origine inconnue)'::text)
               ) AS porte
          FROM datastore_rows r
         WHERE strpos(r.data::text, '(origine inconnue)') > 0) t
 GROUP BY ns_id
 ORDER BY ns_id"""

_BORNE_HAUTE = ("SELECT ns_id, row_id FROM datastore_rows "
                "WHERE (ns_id, row_id) > (%(ns)s, %(rid)s) "
                "ORDER BY ns_id, row_id OFFSET %(saut)s LIMIT 1")
_LOT = ("SELECT ns_id, row_id, data FROM datastore_rows "
        "WHERE (ns_id, row_id) > (%(ns)s, %(rid)s) {haute} "
        "AND strpos(data::text, %(m)s) > 0 ORDER BY ns_id, row_id")
_ECRIRE = ("UPDATE datastore_rows SET data = %(data)s, updated_at = NOW() "
           "WHERE ns_id = %(ns)s AND row_id = %(rid)s")


def _cellule_sans_origine(cellule: dict) -> Any:
    """La case, couche `origine` retirée. Réduite à `valeur` seule, elle redevient
    plate — sauf un objet, qu'on garde enveloppé : déballé, il se lirait comme des
    couches. Vide, elle vaut `null`."""
    reste = {k: v for k, v in cellule.items() if k != ORIGIN_LAYER}
    if not reste:
        return None
    if set(reste) == {VALUE_LAYER} and not isinstance(reste[VALUE_LAYER], dict):
        return reste[VALUE_LAYER]
    return reste


def sans_le_marqueur(data: Any) -> tuple[Any, list[str]]:
    """`(data repris, [colonnes reprises])`. Pur. Une colonne n'est reprise que si sa
    case est un objet dont la couche `origine` EST le marqueur, au type près ; toute
    autre forme reste telle quelle (liste vide = rien à écrire)."""
    if not isinstance(data, dict):
        return data, []
    out, champs = dict(data), []
    for cle, cellule in data.items():
        if isinstance(cellule, dict) and same_value(cellule.get(ORIGIN_LAYER), MARQUEUR):
            out[cle] = _cellule_sans_origine(cellule)
            champs.append(cle)
    return out, sorted(champs)


class LotEnEchec(RuntimeError):
    def __init__(self, depuis: tuple[int, str], cause: BaseException):
        self.depuis = depuis
        super().__init__(f"lot après {depuis[0]}:{depuis[1]} en échec "
                         f"({type(cause).__name__}: {cause})")


def _lot(conn, bas: tuple[int, str], taille: int, *, apply: bool) -> dict:
    """UN lot : sa borne haute (parcours d'index de clé primaire), ses lignes
    préfiltrées, le juge, et en `apply` l'écriture sous verrou. Rend le bilan du lot."""
    for s in _TIMEOUTS:
        conn.execute(s)
    borne = {"ns": bas[0], "rid": bas[1]}
    haute = conn.execute(_BORNE_HAUTE, {**borne, "saut": taille - 1}).fetchone()
    filtre = "AND (ns_id, row_id) <= (%(hns)s, %(hrid)s)" if haute else ""
    sql = _LOT.format(haute=filtre) + (" FOR UPDATE" if apply else "")
    params = {**borne, "m": MARQUEUR}
    if haute:
        params.update(hns=haute["ns_id"], hrid=haute["row_id"])
    reprises, hors_motif = [], []
    for r in conn.execute(sql, params).fetchall():
        neuf, champs = sans_le_marqueur(r["data"])
        if not champs:
            hors_motif.append((r["ns_id"], r["row_id"]))
            continue
        if apply:
            conn.execute(_ECRIRE, {"data": Jsonb(neuf), "ns": r["ns_id"],
                                   "rid": r["row_id"]})
        reprises.append((r["ns_id"], r["row_id"], champs))
    fin = (haute["ns_id"], haute["row_id"]) if haute else None
    return {"reprises": reprises, "hors_motif": hors_motif, "fin": fin}


def executer(*, apply: bool, taille: int = TAILLE_LOT,
             depuis: tuple[int, str] = (0, ""), dire=print) -> dict:
    """Parcourt toute la table par lots. Lève `LotEnEchec` (qui nomme la reprise) au
    premier lot qui échoue ; en `apply`, les lots d'avant restent validés."""
    if taille < 1:
        raise ValueError(f"--taille-lot : au moins 1, reçu {taille}")
    bilan = {"lots": 0, "lignes": 0, "cellules": 0, "par_tableau": {},
             "hors_motif": []}
    bas = depuis
    with geste.interne(SERVICE):
        while True:
            try:
                if apply:
                    with ecriture_de_lignes() as conn:
                        lot = _lot(conn, bas, taille, apply=True)
                else:
                    with _connect() as conn:
                        lot = _lot(conn, bas, taille, apply=False)
                        conn.rollback()
            except Exception as exc:
                raise LotEnEchec(bas, exc) from exc
            bilan["lots"] += 1
            for ns_id, row_id, champs in lot["reprises"]:
                bilan["lignes"] += 1
                bilan["cellules"] += len(champs)
                bilan["par_tableau"][ns_id] = bilan["par_tableau"].get(ns_id, 0) + 1
                dire(f"  {'reprise' if apply else 'à reprendre'} : tableau {ns_id} "
                     f"ligne {row_id} — {', '.join(champs)}")
            for ns_id, row_id in lot["hors_motif"]:
                bilan["hors_motif"].append((ns_id, row_id))
                dire(f"  HORS MOTIF, non touchée : tableau {ns_id} ligne {row_id}")
            if lot["fin"] is None:
                return bilan
            bas = lot["fin"]


def _depuis(texte: str) -> Optional[tuple[int, str]]:
    ns, sep, rid = texte.partition(":")
    return (int(ns), rid) if sep and ns.isdigit() else None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="scripts/purger_origine_inconnue.py",
                                description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="écrit (sinon à blanc)")
    p.add_argument("--taille-lot", type=int, default=TAILLE_LOT)
    p.add_argument("--depuis", help="<ns_id>:<row_id> — reprendre APRÈS cette clé")
    p.add_argument("--constat", action="store_true",
                   help="imprime la requête de constat (lecture seule) et sort")
    a = p.parse_args(argv)
    if a.constat:
        print(f"SET statement_timeout = '15s';\n{CONSTAT_SQL};")
        return 0
    depuis = (0, "")
    if a.depuis:
        depuis = _depuis(a.depuis)
        if depuis is None:
            print(f"--depuis attend <ns_id>:<row_id>, reçu {a.depuis!r}", file=sys.stderr)
            return 2
    print("ÉCRITURE (--apply)" if a.apply else "À BLANC — rien ne sera écrit")
    try:
        bilan = executer(apply=a.apply, taille=a.taille_lot, depuis=depuis)
    except ValueError as refus:
        print(str(refus), file=sys.stderr)
        return 2
    except LotEnEchec as echec:
        print(f"\nÉCHEC : {echec}", file=sys.stderr)
        if a.apply:
            print("les lots précédents sont VALIDÉS ; reprendre par :\n  lanceur --script "
                  "scripts/purger_origine_inconnue.py --apply --depuis "
                  f"{echec.depuis[0]}:{echec.depuis[1]}", file=sys.stderr)
        return 4
    print(f"\n{bilan['lots']} lot(s) de {a.taille_lot} ; {bilan['lignes']} ligne(s), "
          f"{bilan['cellules']} case(s) {'reprises' if a.apply else 'à reprendre'} ; "
          f"{len(bilan['hors_motif'])} ligne(s) hors motif, non touchées")
    for ns_id, n in sorted(bilan["par_tableau"].items()):
        print(f"  tableau {ns_id} : {n} ligne(s)")
    if not a.apply:
        print("passe à blanc — rien n'est écrit (--apply pour écrire)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

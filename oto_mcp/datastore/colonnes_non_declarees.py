"""Écrire dans une colonne NON DÉCLARÉE est REFUSÉ, sur tous les tableaux (oto#124).

Une écriture qui nommait une colonne que le schéma ne déclare pas la créait à la volée :
le schéma d'un tableau s'étendait au fil des écritures, sans geste dédié. Arbitré le
07/09/2026 (« ça fait n'importe quoi dans les flottes d'agents »), précisé le 05/10 :
**une seule règle, plus de modes**. Une colonne inconnue est REFUSÉE, sur TOUS les
tableaux ; pour écrire dans une colonne nouvelle, on la déclare d'abord
(`data_patch_schema`). En vigueur depuis le 21/10/2026, après un préavis daté ; les
colonnes que portaient les tableaux ont été déclarées pour eux avant la bascule
(`scripts/figer_colonnes.py`).

`ColonneNonDeclaree` — `400 unknown_column` côté REST et réception d'upload,
`INVALID_PARAMS` côté MCP —, rien n'est écrit. Un lot (et un import découpé en tranches)
est jugé ENTIER avant sa première ligne : le lot n'étant pas atomique, le juger ligne à
ligne laisserait sa première moitié écrite.

Le prédicat est UN : `non_declarees`, sur ce que le geste POSE — jamais sur la ligne
fusionnée. Un patch qui touche une colonne déclarée d'une ligne portant des colonnes
anciennes non déclarées ne se juge pas sur elles (le cran juge le geste, pas le passé
qu'il hérite, #284). Effacer une colonne non déclarée (`null`) reste possible : elle
n'est plus posée.

⚠️ **Ce que ce module ne touche PAS** : la validation complète du FORMAT (`options` de
premier niveau, structure des types, couches inconnues, fermeture des sous-records,
gardes de pose de `claimable`) — c'est `validation_complete`.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .couches import split_layer
from .declaration import _fields
from .errors import ColonneNonDeclaree

#: Ce qu'un refus cite avant d'abréger.
_CITEES = 8
_REFERENTIEL_CITE = 15


# ── le prédicat ──────────────────────────────────────────────────────────────

def declarees(schema: Optional[dict]) -> set:
    """Les colonnes de PREMIER niveau que le schéma déclare (vide sans schéma)."""
    return {f["key"] for f in _fields(schema)
            if isinstance(f.get("key"), str) and f["key"]}


def non_declarees(schema: Optional[dict], posees: Optional[dict]) -> list[str]:
    """Les colonnes que le geste POSE et que le schéma ne déclare pas, triées.

    Sur un tableau sans schéma — ou un schéma sans colonne —, TOUTE colonne posée l'est :
    c'est la règle commune, et ce qui la rend vivable est le gel
    (`scripts/figer_colonnes.py`). `<colonne déclarée>.<couche>` n'est pas une colonne
    (même règle que `hors_schema._unknown_subkeys`). Un `null` EFFACE, il ne crée rien :
    vider une colonne non déclarée reste possible — c'est le geste qui la retire."""
    if not isinstance(posees, dict):
        return []
    connues = declarees(schema)
    out = []
    for cle, valeur in posees.items():
        if not isinstance(cle, str) or cle in connues or valeur is None:
            continue
        base, couche = split_layer(cle)
        if couche and base in connues:
            continue
        out.append(cle)
    return sorted(out)


_BASE = re.compile(r"[.\[]")


def non_declarees_du_lot(schema: Optional[dict], rows: Iterable[Any]) -> list[str]:
    """Les colonnes non déclarées qu'un LOT poserait, jugées sur les clés BRUTES des
    lignes avant leur premier écrit — `{colonne: None}` exclus.

    Une clé pointée ou par rang (`site_web.comment`, `contacts[0].email`) vise la
    colonne de sa BASE : elle compte comme non déclarée si sa base l'est. Les clés
    techniques (`_id`, `_revision`…) ne sont pas des colonnes ; leurs refus propres
    viennent ensuite, ligne par ligne, comme celui d'un nom vide ou blanc, qui n'est pas
    un nom de colonne (#231, `points._refuse_dotted_names`) : le déclarer serait
    impossible. Un `null` sur une colonne non déclarée l'EFFACE, il n'en crée pas, et un
    `{}` n'est pas une valeur (oto#165, écarté avant d'écrire) : ni l'un ni l'autre
    n'est compté (même règle que `non_declarees`)."""
    connues = declarees(schema)
    out: set = set()
    for data in rows:
        if not isinstance(data, dict):
            continue
        for cle, valeur in data.items():
            if (not isinstance(cle, str) or not cle.strip() or cle.startswith("_")
                    or cle in connues):
                continue
            base = _BASE.split(cle, 1)[0]
            if base in connues or valeur is None or valeur == {}:
                continue
            out.add(base or cle)
    return sorted(out)


# ── ce que la réponse en dit, et le refus ────────────────────────────────────

def _noms(colonnes: list[str]) -> str:
    cite = ", ".join(f"`{c}`" for c in colonnes[:_CITEES])
    reste = len(colonnes) - _CITEES
    return cite + (f" (+{reste} more)" if reste > 0 else "")


def _geste(colonnes: list[str]) -> str:
    champs = ", ".join(f'{{"key": "{c}"}}' for c in colonnes[:3])
    return (f"declare it in the schema first — `data_patch_schema(datastore=…, "
            f"fields=[{champs}])` (REST: `PATCH /api/datastores/{{datastore}}/schema`, "
            f"body `{{\"fields\": [{champs}]}}`), with its `type` if you know it —, "
            f"then write")


def refus(schema: Optional[dict], colonnes: list[str], *,
          lot: bool = False) -> ColonneNonDeclaree:
    """Le refus : les colonnes, le référentiel, le geste.

    ⚠️ Aucune destination n'est DEVINÉE (#678) : pointer la colonne « la plus proche »
    enverrait la valeur dans une colonne juste. Seule exception, qui ne devine rien
    (oto#63) : `<colonne déclarée>_<couche>` est une couche écrite avec un souligné au
    lieu d'un point — la destination se LIT dans la clé (`couche_mal_ecrite`)."""
    from .hors_schema import couche_mal_ecrite

    connues = [f["key"] for f in _fields(schema)
               if isinstance(f.get("key"), str) and f["key"]]
    details: dict = {"colonnes": list(colonnes)}
    couches = [(k, *c) for k in colonnes if (c := couche_mal_ecrite(k, set(connues)))]
    if couches:
        k, col, couche = couches[0]
        details["expected_column"] = f"{col}.{couche}"
        return ColonneNonDeclaree(
            [f"{_noms(colonnes)}: nothing was written. ⚠️ This is not an unknown "
             f"column, it is a LAYER whose name is written with a DOT — "
             + " ; ".join(f"`{a}` → `{b}.{c}`" for a, b, c in couches)
             + f". `{{\"{col}\": {{\"{couche}\": …}}}}` writes the same thing, and it is "
             f"the form `layers=\"nested\"` returns to you on read."],
            details=details)
    if connues:
        cite = ", ".join(f"`{k}`" for k in connues[:_REFERENTIEL_CITE])
        if len(connues) > _REFERENTIEL_CITE:
            cite += f" (+{len(connues) - _REFERENTIEL_CITE} more, `data_get_schema`)"
        referentiel = f"Table columns: {cite}."
    else:
        referentiel = "This table does not declare any column yet."
    pluriel = len(colonnes) > 1
    portee = "this batch is refused ENTIRELY, " if lot else ""
    return ColonneNonDeclaree(
        [f"{_noms(colonnes)}: column{'s' if pluriel else ''} not "
         f"declared — {portee}nothing was written. Writing to a column does not "
         f"create it, on any table. "
         f"{referentiel} Write under a declared name; or, if the column is intended, "
         f"{_geste(colonnes)}. ⚠️ Do not retry under a variant of the same name: "
         f"it would be refused the same way."],
        details=details)


def controler(schema: Optional[dict], posees: Optional[dict]) -> None:
    """Le geste pose-t-il une colonne non déclarée ? REFUS."""
    colonnes = non_declarees(schema, posees)
    if colonnes:
        raise refus(schema, colonnes)


def juger_le_lot(schema: Optional[dict], rows: Iterable[Any]) -> None:
    """Un lot qui poserait une colonne non déclarée est refusé ENTIER, avant sa
    première écriture."""
    colonnes = non_declarees_du_lot(schema, rows)
    if colonnes:
        raise refus(schema, colonnes, lot=True)


# ── le texte servi ───────────────────────────────────────────────────────────

_GESTE_EN = ("To write a NEW column, declare it first — `data_patch_schema(datastore=…, "
             "fields=[{\"key\": \"<column>\", \"type\": …}])` — then write.")

#: L'annonce servie dans la description de `data_write` et des faces REST.
DESCRIPTION_ECRITURE = ("⚠️ **A write into a column the schema does NOT declare is REFUSED** "
                        "(`unknown_column`), on every table "
                        f"— nothing is written, a batch is refused WHOLE. {_GESTE_EN}")


def description_schema() -> str:
    """Ce que `data_set_schema` et `data_patch_schema` disent de la règle : déclarer une
    colonne est le SEUL moyen d'en ajouter une."""
    return ("⚠️ A column the schema does NOT declare is REFUSED at write, on EVERY "
            "table: declaring a column (`data_patch_schema(fields=[{\"key\": …}])`) is "
            "the ONLY way to add one.")


def description_creation() -> str:
    """Ce que `data_create_datastore` et `POST /api/datastores` disent du paramètre
    `schema` : un tableau NAÎT sans colonne, donc sa première écriture est refusée tant
    qu'on ne lui en a pas déclaré. Le déclarer à la création est le geste normal pour un
    tableau qu'on va remplir."""
    return ("⚠️ **A new table is born with NO column, and a write into a column the "
            "schema does not declare is REFUSED** (`unknown_column`): its first write is "
            "refused until columns are declared. To "
            "create a table you are going to FILL, pass `schema` — the SAME object as "
            "`data_set_schema` (`{\"fields\": [{\"key\": …, \"type\": …}], \"key\"?: …}`, "
            "same closed vocabulary, same validation): the table is born with it, in ONE "
            "atomic call. If the schema is refused, the table is NOT created. That is the "
            "normal way to create a table you fill; without `schema` it is born free, "
            "and `data_patch_schema(fields=[…])` declares columns later.")

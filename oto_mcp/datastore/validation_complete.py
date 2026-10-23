"""Le format déclaré fait contrat sur TOUS les tableaux (oto#124).

La validation COMPLÈTE d'une écriture était un réglage (`unknown_columns` autre que
`create`). Elle fait respecter, en plus de ce qui s'arme seul (`required`, bornes, motif,
types armés, couches exigées, cycle de vie) :

- les `options` de premier niveau ;
- la FORME des valeurs `text`, `url`, `object`, `list` et `enum` ;
- les couches inconnues d'une valeur (`{"valeur": …, "commentaire": …}`) ;
- la fermeture des sous-records déclarés (un attribut que `fields` ne nomme pas) ;
- à la pose, la garde de `lifecycle.claimable` (une colonne non déclarée).

Arbitré le 05/10/2026 : **toujours refuser, plus aucun réglage**. En vigueur sur tout
tableau qui déclare une colonne depuis le 21/10/2026, après un préavis daté ;
`unknown_columns` est refusé à la pose (`schema_keys.CLES_RETIREES`).

On ne corrige PAS les données déjà en faute — et on ne les bloque pas : **on juge ce
que le geste ÉCRIT, jamais ce que la ligne porte déjà** (`validation._row_errors`,
`pose`). Une ligne en faute s'écrit sur ses autres colonnes ; la faute qu'elle porte se
DIT (`hors_type`), elle ne refuse rien. Le refus : `row_invalid`, ou la valeur hors
options ÉCARTÉE quand elle est la seule faute (#667).
"""
from __future__ import annotations

from typing import Any


def complete(schema: Any) -> bool:
    """LA décision : la validation complète s'applique-t-elle à ce tableau ? — sur tout
    tableau qui déclare au moins une colonne."""
    champs = schema.get("fields") if isinstance(schema, dict) else None
    return isinstance(champs, list) and any(
        isinstance(f, dict) and isinstance(f.get("key"), str) and f["key"]
        for f in champs)


# ── le texte servi ───────────────────────────────────────────────────────────

_REGLE_EN = ("top-level `options` are enforced, the SHAPE of `text`/`url`/`object`/"
             "`list`/`enum` values is checked, an unknown layer is refused and a "
             "declared sub-record refuses an attribute it does not declare")
_JUGE_EN = ("Only what a write SETS is judged: a row already off-format still accepts "
            "a write to its OTHER columns (the stale value is reported in `hors_type`).")


def description_ecriture() -> str:
    """L'annonce servie dans la description de `data_write` et des faces REST."""
    return (f"⚠️ **The declared format is a CONTRACT on every table**: {_REGLE_EN}. "
            f"A violation is refused (`row_invalid`; an off-options value alone in "
            f"its fault is set aside and the rest of the row is written). "
            f"{_JUGE_EN} No head setting changes this.")


def description_schema() -> str:
    """Ce que `data_set_schema` et `data_patch_schema` disent de la règle — et de la fin
    du réglage `unknown_columns`."""
    return (f"⚠️ The declared format is a CONTRACT on every table: {_REGLE_EN}. "
            "There is NO head setting for it any more: `unknown_columns` was REMOVED "
            "on 2026-10-05 and is refused — columns and values are always checked.")

"""Une vue FILTRÉE d'un tableau, portée par son adresse (signal 1355).

La page tableau du tableau de bord lit sa vue dans l'URL à l'ouverture — c'est ce qui
la rend partageable par lien (oto-app, `DatastoreTable.vue::readTableQuery`) :

    f      les filtres, en JSON : une liste de triplets `[champ, op, valeur]`
           (`lib/datastoreFilters.ts::filtersFromParam`) — `valeur` une chaîne, ou une
           liste pour `in`, vide pour `empty`/`not_empty` ;
    q      la recherche ;
    sort   la colonne de tri, `dir` = `asc` | `desc`.

Ce module écrit ces paramètres depuis la grammaire de `data_rows` (`filter`), et refuse
ce que la page ÉCARTERAIT sans rien dire : elle ignore un triplet dont l'opérateur est
inconnu ou dont la valeur est vide — le lien montrerait alors plus de lignes qu'il ne le
prétend. Seule la page tableau de notre tableau de bord lit ce format
(`links.table_lit_sa_vue`).
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..db.query import ds_filter_specs

#: Les opérateurs qui ne portent pas de valeur : la page les relit sans.
_SANS_VALEUR = frozenset({"empty", "not_empty"})


def _valeur(champ: str, op: str, valeur):
    if op in _SANS_VALEUR:
        return ""
    if op == "in":
        if not isinstance(valeur, (list, tuple)) or not valeur:
            raise ValueError(f"filter `{champ}`: `in` takes a non-empty list")
        return [str(v) for v in valeur]
    if valeur is None or valeur == "" or isinstance(valeur, (dict, list)):
        raise ValueError(
            f"filter `{champ}`: `{op}` needs a single non-empty value — the table page "
            f"drops it otherwise, and the link would show every row. For rows with no "
            f"value, use {{\"{champ}\": {{\"empty\": true}}}}.")
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    return str(valeur)


def triplets(filter: Optional[dict]) -> list[list]:
    """`filter` (forme de `data_rows`) → les triplets que la page relit. Lève
    `ValueError` sur une valeur qu'elle écarterait ; l'opérateur, lui, est jugé par le
    moteur (`count_rows`, appelé AVANT par `data_url`), qui connaît les mêmes que la
    page (`db.query._DS_FILTER_OPS`, miroir côté tableau de bord)."""
    out = []
    for spec in ds_filter_specs(filter):
        champ, op = spec["field"], spec["op"]
        out.append([champ, op, _valeur(champ, op, spec["value"])])
    return out


def avec_la_vue(url: str, *, filter: Optional[dict], q: Optional[str],
                order_by: Optional[str], order_dir: str) -> str:
    """L'adresse du tableau, augmentée de la vue. `order_dir` hors `asc`/`desc` lève."""
    if order_dir not in ("asc", "desc"):
        raise ValueError(f"`order_dir` is `asc` or `desc`, not {order_dir!r}")
    vue: dict[str, str] = {}
    if q:
        vue["q"] = q
    if order_by:
        vue["sort"], vue["dir"] = order_by, order_dir
    t = triplets(filter)
    if t:
        vue["f"] = json.dumps(t, ensure_ascii=False, separators=(",", ":"))
    p = urlsplit(url)
    query = "&".join(x for x in (p.query, urlencode(vue, quote_via=quote)) if x)
    return urlunsplit((p.scheme, p.netloc, p.path, query, p.fragment))

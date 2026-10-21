"""AJOUTER une ligne dont la clé existe déjà ne la FUSIONNE plus en silence (oto#141).

Quand un tableau déclare une clé métier, une écriture SANS `id` dont la valeur de clé
existait déjà mettait la ligne en place à jour en silence — ligne seule, lot, REST,
upload signé. Et dans un lot, deux lignes à la même clé fusionnaient entre elles :
`ids: [r1, r1, r2, r3]` pour trois lignes écrites.

Arbitré le 30/09/2026, précisé le 01/10 : tout dépend de ce que l'appel DIT de la clé.

- **Désigner** — par `id=`, ou par `key=` passé EXPLICITEMENT à l'appel (lot, ligne seule
  qui nomme la clé déclarée, clé scellée au mint d'un upload), ou sur un tableau FERMÉ
  (`new_rows: "reject"`, où une écriture sans `id` ne peut que viser) : une valeur de clé déjà
  présente MODIFIE sa ligne, sans `upsert` et sans un mot ; une valeur absente crée la
  ligne (sauf tableau fermé, dont le refus de création s'applique).
- **Ajouter** — ni `id` ni `key=`, sur un tableau qui déclare une clé : une valeur déjà
  présente est un doublon que l'appelant n'a pas vu. REFUS `business_key_exists`, qui
  nomme la ligne en place — sauf `upsert=true`, qui demande la fusion.
- **Deux lignes du même appel à la même clé** : on ne désigne pas deux fois la même
  ligne dans un appel. Sans `upsert`, le lot est REFUSÉ ENTIER — en désignation comme
  en ajout.

En vigueur depuis le 21/10/2026, après un préavis daté.

Un lot rend chaque fusion dans `fusions` — `{rang, dans_rang, id, cle}`, `dans_rang` =
le rang de la ligne du MÊME geste qui a posé la ligne visée, `null` pour une ligne déjà
en base —, et `ids` reste aligné rang pour rang.

⚠️ **La règle de comparaison n'est PAS ici.** Qu'une valeur « existe déjà », c'est
`db.datastore_find_row_id_by_key` qui le dit, et rien d'autre. Deux lignes d'un même lot
« à la même clé » se jugent sur le texte que la BASE rend de leur valeur déballée
(`db.datastore_textes_de_cle`, la conversion du lookup, oto#223) — jamais sur une
conversion Python.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from . import schema as dsv2
from .errors import BusinessKeyExists

#: Au-delà, un refus de lot cite les premiers cas et compte les autres.
_CITES = 5


# ── le texte servi ───────────────────────────────────────────────────────────

_GESTES_EN = ("To DESIGNATE a row, pass its `id=`, or name the business key with `key=` "
              "(a batch, or a single row naming the declared key): an existing key value "
              "then MODIFIES its row and a new one creates it, no `upsert` needed. Without "
              "`id` or `key=`, a write ADDS a row.")

_FUSIONS_EN = ("A batch reports every merge in `fusions` — `{rang, dans_rang, id, cle}`, "
               "`dans_rang` being the earlier row of the SAME batch that made the target "
               "row, `null` for a row already in the table — and `ids` stays aligned row "
               "for row.")

_FERME_EN = ("On a CLOSED table (`new_rows: \"reject\"`) every write without `id` designates "
             "by the key.")


#: L'annonce servie dans la description de `data_write`.
DESCRIPTION_ECRITURE = (
    "⚠️ **Business key: ADDING a row whose key value already exists is "
    "REFUSED** (`business_key_exists`, naming the existing row) unless you "
    f"pass `upsert=true`, which merges onto it. {_GESTES_EN} Two rows of one "
    "call with the same key value refuse the WHOLE batch, before anything is "
    f"written, unless `upsert=true`. {_FUSIONS_EN} {_FERME_EN}")


def description_cle_schema() -> str:
    """Ce que la clé métier DÉCLARÉE fait à l'écriture, pour `data_set_schema` et
    `data_patch_schema` — la phrase qui disait « EVERY write … then UPSERTs on it »."""
    return ("A write that DESIGNATES by the key — `key=` passed to the call (a batch, "
            "or a single row naming this key), or any write without `id` on a CLOSED "
            "table — modifies the row of an existing key value and creates a new one. A "
            "write that ADDS (neither `id` nor `key=`) with a key value already present "
            "is REFUSED unless it passes `upsert=true`, which merges onto that "
            "row — `data_write`, REST and `oto_upload_url` alike. The key never "
            "duplicates: its UNIQUE index guarantees it.")


def description_parametre() -> str:
    """Le paramètre `upsert`, tel que le servent les faces (MCP, REST, upload)."""
    return ("`true` = MERGE when the key value already exists — for a write that ADDS "
            "(neither `id` nor `key=`), and for rows of one call that share a key value. "
            "A write that designates by `key=` needs no `upsert`. Every merge of a batch "
            "comes back in `fusions`. Without it, an adding write on an existing key, "
            "or two rows of one call on the same key, is REFUSED (`business_key_exists`). "
            "Refused on a table with "
            "no business key: there is nothing to merge on.")


# ── ce que l'écriture en fait ─────────────────────────────────────────────────

def designe(schema: Optional[dict], cle_passee: bool) -> bool:
    """L'appel DÉSIGNE-t-il par la clé ? `key=` passé explicitement, ou un tableau FERMÉ
    — qui ne crée jamais, donc où une écriture sans `id` ne peut que viser (son refus de
    création le conseille, `outils._refus_de_creation`)."""
    return bool(cle_passee) or dsv2.creation_refusee(schema)


def valeur_de_cle(data: Any, key: Optional[str]) -> Any:
    """La valeur de clé qu'un lot CHERCHE pour cette ligne, ou None — déballée (une clé
    annotée est la même identité qu'une clé nue), et vide = aucune (cf. `lots.py`)."""
    if not key or not isinstance(data, dict):
        return None
    kv = dsv2.unwrap(data.get(key))
    return None if kv is None or str(kv) == "" else kv


def refuser_upsert_sans_cle(upsert: bool, key: Optional[str], datastore: str, *,
                            lot: bool = False) -> None:
    """`upsert=true` sans clé en vigueur ne fusionnerait RIEN, et l'appelant croirait
    avoir dédoublonné : refusé, comme tout paramètre offert qui ne serait pas réglé."""
    if upsert and not key:
        raise ValueError(
            f"`upsert=true` has nothing to merge on: `{datastore}` declares no "
            f"business key{', and this batch does not pass `key=`' if lot else ''}. Nothing was "
            "written. Declare it (data_patch_schema(key=…))"
            f"{' or pass `key=` to the batch' if lot else ''}; otherwise remove `upsert`.")


def _geste(key: str, row_id: Optional[str] = None) -> str:
    cible = f"`id={row_id}`" if row_id else "its `id`"
    return (f"To MODIFY the row in place, designate it: {cible}, or `key={key!r}`; "
            "to merge an addition, pass `upsert=true`.")


def refus_unitaire(datastore: str, key: str, kv: Any, row_id: str, *,
                   nommer: bool = True) -> BusinessKeyExists:
    """Le refus d'UN ajout : la valeur, la ligne qu'elle désigne, les gestes.
    `nommer=False` : lu par un porteur de lien anonyme (upload signé) — l'identifiant
    interne de la ligne ne sort pas (oto#86)."""
    ligne = f"row `{row_id}`" if nommer else "a row"
    return BusinessKeyExists(
        f"this write ADDS a row (neither `id` nor `key=`), but `{key}` = "
        f"{str(kv)!r} already designates {ligne} of `{datastore}`: nothing was written for "
        f"it. An addition does not merge by itself on the business key. "
        f"{_geste(key, row_id if nommer else None)}",
        key=key, details={"key": key, "valeur": kv,
                          **({"id": row_id} if nommer else {})})


def refus_de_doublon(datastore: str, key: str, kv: Any, rangs: list[int]) -> BusinessKeyExists:
    """Le refus d'une ligne qui retrouve, PENDANT le lot, une ligne du même lot — une
    course, le lot ayant été jugé entier avant sa première ligne."""
    return BusinessKeyExists(
        f"rows {_rangs(rangs)} of this call: same {key} {str(kv)!r} — the same row "
        f"is not designated twice in one call: this is refused without "
        "`upsert=true`. Remove the duplicate from the batch, or pass `upsert=true` to "
        "merge.", key=key,
        details={"key": key, "doublons": [{"rangs": rangs, "valeur": kv}],
                 "existantes": []})


def controler_fusion(*, designation: bool, upsert: bool,
                     datastore: str, key: str, kv: Any, row_id: str,
                     dans_rang: Optional[int] = None, rang: Optional[int] = None,
                     nommer: bool = True) -> None:
    """Une écriture sans `id` s'apprête à fusionner dans `row_id` par sa clé.

    `upsert` : demandé, rien à dire. `dans_rang` renseigné : la ligne visée vient d'être
    posée par le MÊME appel — un doublon, en désignation comme en ajout : refusé. Sinon
    la ligne était en base : une `designation` la modifie sans un mot, un AJOUT est
    refusé. Sur un lot, seule une COURSE arrive encore ici, le lot ayant été jugé entier
    avant sa première ligne."""
    if upsert:
        return
    if dans_rang is not None:
        raise refus_de_doublon(datastore, key, kv, [dans_rang, rang])
    if designation:
        return
    raise refus_unitaire(datastore, key, kv, row_id, nommer=nommer)


def _rangs(rangs: list[int]) -> str:
    tete = ", ".join(str(r) for r in rangs[:-1])
    return f"{tete} and {rangs[-1]}"


def _majuscule(texte: str) -> str:
    return texte[:1].upper() + texte[1:]


def _cites(phrases: list[str]) -> str:
    reste = len(phrases) - _CITES
    return " ".join(phrases[:_CITES]) + (f" … and {reste} more." if reste > 0 else "")


def juger_le_lot(rows: Iterable[Any], *, key: str, datastore: str, designation: bool,
                 chercher: Callable[[Any], Optional[str]],
                 textes: Callable[[list], list[str]], decalage: int = 0,
                 nommer: bool = True) -> None:
    """Sans `upsert`, le lot ENTIER est jugé avant sa première
    écriture : ses doublons internes toujours, et — s'il AJOUTE (`designation` fausse) —
    ses lignes dont la clé est déjà en base. Un refus ici n'a rien écrit — le lot n'étant
    pas atomique, le juger ligne à ligne laisserait sa première moitié écrite.

    `chercher(valeur)` = le lookup de clé du store (`db.datastore_find_row_id_by_key`),
    appelé une fois par valeur distincte ; `textes(valeurs)` = le texte que la base
    rend de chaque valeur (`db.datastore_textes_de_cle`), sur lequel deux lignes du lot
    sont « la même clé ». `decalage` = le rang absolu de la ligne qui
    précède ce lot (import découpé en tranches). `nommer=False` : l'identifiant interne
    des lignes en place ne sort pas (porteur de lien anonyme, oto#86)."""
    premiers: dict[str, int] = {}
    doublons: dict[str, list[int]] = {}
    valeurs: dict[str, Any] = {}
    existantes: list[dict] = []
    portees = [(decalage + i, kv) for i, kv in
               enumerate((valeur_de_cle(d, key) for d in rows), 1) if kv is not None]
    for (rang, kv), compare in zip(portees, textes([kv for _, kv in portees])):
        if compare in premiers:
            doublons.setdefault(compare, [premiers[compare]]).append(rang)
            continue
        premiers[compare], valeurs[compare] = rang, kv
        if designation:
            continue
        row_id = chercher(kv)
        if row_id is not None:
            existantes.append({"rang": rang, "valeur": kv,
                               **({"id": row_id} if nommer else {})})
    if not doublons and not existantes:
        return
    phrases = [f"Rows {_rangs(r)}: same {key} {str(valeurs[c])!r}."
               for c, r in doublons.items()]
    phrases += [f"Row {e['rang']}: {key} {str(e['valeur'])!r} already designates "
                + (f"row `{e['id']}`." if nommer else "a row of the table.")
                for e in existantes]
    regles = []
    if doublons:
        regles.append("the same row is not designated twice in one call")
    if existantes:
        regles.append("an ADDITION (neither `id` nor `key=`) does not merge by itself on the "
                      "business key")
    gestes = (["a duplicate is removed from the batch"] if doublons else []) + (
        [f"to modify the rows in place, designate them: `key={key!r}`"]
        if existantes else []) + ["to merge, pass `upsert=true`"]
    raise BusinessKeyExists(
        f"batch refused ENTIRELY, nothing was written: "
        f"{'; '.join(regles)} (`{key}` of `{datastore}`). {_cites(phrases)} "
        f"{_majuscule('; '.join(gestes))}.",
        key=key,
        details={"key": key,
                 "doublons": [{"rangs": r, "valeur": valeurs[c]}
                              for c, r in doublons.items()],
                 "existantes": existantes})


class Fusions:
    """Le relevé des fusions d'UN geste — un lot, ou un import découpé en tranches qui
    partagent le même relevé pour que `dans_rang` traverse les tranches.

    `decalage` = rang absolu de la ligne qui précède la tranche en cours ; `juge` = le
    geste a déjà été jugé ENTIER (`juger_le_lot`) par qui le découpe ; `nommer=False` =
    lu par un porteur de lien ANONYME (l'accusé d'un upload signé) : ni `fusions` ni
    les refus n'y portent l'identifiant interne d'une ligne (oto#86)."""

    def __init__(self, *, nommer: bool = True) -> None:
        self.decalage = 0
        self.juge = False
        self.nommer = nommer
        self._premier_rang: dict[str, int] = {}

    def creee(self, rang: int, row_id: str) -> None:
        self._premier_rang.setdefault(row_id, self.decalage + rang)

    def fusion(self, rang: int, row_id: str, colonne: str, valeur: Any) -> dict:
        """L'entrée de `fusions` : `dans_rang` = la ligne du même geste qui a posé (ou
        déjà visé) `row_id`, `None` si elle était en base avant lui."""
        absolu = self.decalage + rang
        entree = {"rang": absolu, "dans_rang": self._premier_rang.get(row_id),
                  **({"id": row_id} if self.nommer else {}), "cle": {colonne: valeur}}
        self._premier_rang.setdefault(row_id, absolu)
        return entree

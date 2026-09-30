"""Ce que les lignes EN PLACE violent déjà du schéma qu'on pose — le relevé structuré
`existing_violations` (oto-backend#479).

Cas fondateur (28/08/2026) : une garde posée sur un sous-champ de liste d'un tableau de
8 910 lignes, dont 854 portaient déjà une valeur qu'elle condamnait. La plateforme
appliquait bien la règle ; c'est l'ORDRE des gestes qui piégeait — nettoyer l'existant
avant d'armer la garde, ce que rien ne disait au moment où on armait.

Les avertissements de la pose (`schema_ops.set_schema`) disaient déjà quatre crans en
PHRASES (`required`, `max_length`, `pattern`, options). Il en restait de muets — le requis
conditionnel `required_when`, les sous-champs de liste, le type déclaré, la clé métier —
et aucune forme qu'un appelant puisse exploiter pour nettoyer avant d'armer. Ce module
rend, par CHEMIN (`contacts[].email`), le nombre de lignes fautives, un échantillon de
leurs identifiants et la conséquence en clair.

**Un seul moteur.** Chaque ligne est jugée par `validate_row`, celui qui refusera les
écritures — jamais par un second prédicat (SQL ou autre) qui compterait selon d'autres
règles : un avertissement qui annonce un nombre que le refus ne confirme pas est pire
que pas d'avertissement. D'où, aussi, aucun cran listé dans le JUGEMENT : ce que le
moteur fait respecter demain sera relevé demain. Seul le garde `arme` (faut-il balayer
le tableau ?) nomme ce qui s'arme, et son banc le confronte à toutes les sondes de
`enforced`. Deux jugements par ligne, qui séparent les deux conséquences possibles :

- la ligne RÉÉCRITE en entier (`written=None`) : tout ce que le format condamne ;
- une écriture d'une AUTRE colonne (`written=set()`) : ce qui BLOQUE la ligne — refusé
  quel que soit le champ visé. Le reste ne se refuse qu'au geste qui réécrit le champ,
  et se signale en passant (`hors_type`).

⚠️ **Coût borné, et la borne se DIT.** Au-delà de `PLAFOND_LIGNES` lignes examinées, le
relevé s'arrête et `existing_violations_scope.complete` vaut `false` : les comptes sont
alors des PLANCHERS. Un relevé tronqué en silence transformerait « je n'ai pas tout
regardé » en « il n'y a rien ».

Ce qu'il ne tient pas : la décision de refuser (`validation.py`), les avertissements en
phrases de la pose (`schema_ops.py`), le refus des doublons de clé métier (à la pose,
`set_schema`).
"""
from __future__ import annotations

from typing import Any, Optional

from .. import db
from . import schema as dsv2
from .etats_declares import colonnes_a_etats
from .types_declares import TYPES_ARMES

#: Lignes examinées au plus par une pose — de quoi couvrir le tableau du cas fondateur
#: (8 910 lignes) en entier. Au-delà, le relevé est partiel et le dit.
PLAFOND_LIGNES = 10_000
#: Taille d'une page lue en base : le relevé ne tient jamais plus d'une page en mémoire.
PAGE = 500
#: Identifiants rendus par chemin — de quoi aller voir, pas de quoi recopier le tableau.
ECHANTILLON = 5

BLOQUANTE = "bloquante"
AU_GESTE = "au_geste"
SANS_CLE = "sans_cle"

_CONSEQUENCES = {
    BLOQUANTE: ("n'acceptent PLUS AUCUNE écriture, sur aucune colonne, tant que ce champ "
                "n'est pas corrigé — et le refus nommera ce champ, pas celui qu'on "
                "écrivait"),
    AU_GESTE: ("ne passent plus le format : le geste qui réécrit ce champ est refusé "
               "(une valeur hors options y est écartée, le reste de la ligne s'écrit) ; "
               "une écriture d'une autre colonne passe, et signale la valeur dans "
               "`hors_type`"),
    SANS_CLE: ("ne portent pas la clé métier : aucune écriture par clé ne les retrouve, "
               "seule une écriture par `id` les atteint"),
}


def chemin_du_refus(refus: str) -> str:
    """Le chemin de la faute, tel que le moteur l'a posé en tête de son refus :
    `contacts[3].email: …` → `contacts[].email`. Le rang d'un élément ne dit rien de
    la déclaration fautive, qui vaut pour toute la liste.

    ⚠️ Deux formes, et deux seulement : `<chemin>: …` (tout `_row_errors`, les couches
    exigées, les états) et `` `<colonne>` est déclarée … `` (`types_trahis`). C'est la
    lecture que `_type_error` fait déjà de ses propres refus pour son relevé `gelees` ;
    le banc de ce module fixe chaque famille, pour qu'un refus reformulé casse un test
    plutôt que ce relevé. Une clé de colonne qui contiendrait `": "` s'y lirait coupée."""
    tete = (refus[1:].split("`", 1)[0] if refus.startswith("`")
            else refus.split(": ", 1)[0])
    texte, dedans = [], False
    for c in tete:
        if c == "[":
            dedans = True
            texte.append("[]")
        elif c == "]":
            dedans = False
        elif not dedans:
            texte.append(c)
    return "".join(texte)


def juger_ligne(schema: Optional[dict], data: dict) -> dict[str, str]:
    """`{chemin: nature}` des fautes d'UNE ligne en place contre `schema`.

    Réécrite en entier, la ligne dit tout ce que le format condamne ; sous une écriture
    d'une AUTRE colonne, ce qui reste refusé la BLOQUE — et ce chemin-là l'emporte."""
    if not isinstance(data, dict):
        return {}
    fautes: dict[str, str] = {}
    cle = (schema or {}).get("key")
    if isinstance(cle, str) and cle and dsv2.unwrap(data.get(cle)) in (None, ""):
        fautes[cle] = SANS_CLE
    reecrite = dsv2.validate_row(schema, data)
    if not reecrite:
        return fautes
    for refus in reecrite:
        fautes[chemin_du_refus(refus)] = AU_GESTE
    for refus in dsv2.validate_row(schema, data, written=set()):
        fautes[chemin_du_refus(refus)] = BLOQUANTE
    return fautes


def _consequence(natures: dict[str, int]) -> str:
    return " ; ".join(f"{n} ligne(s) {_CONSEQUENCES[nature]}"
                      for nature, n in natures.items())


def arme(schema: Optional[dict]) -> bool:
    """Ce schéma arme-t-il un refus que l'existant pourrait déjà violer ?

    Sans lui, le relevé balaierait le tableau pour conclure « rien » à chaque pose —
    un libellé changé coûterait dix mille lignes jugées. La liste suit ce que
    `validate_row` fait respecter hors de `validation_active` ; le banc la confronte à
    toutes les sondes de `enforced` (`vocabulaire._ENFORCEMENT_PROBES`), pour qu'un
    cran neuf que ce garde ignorerait casse un test plutôt que de se taire ici."""
    if not isinstance(schema, dict):
        return False
    if dsv2.validation_active(schema) or dsv2.lifecycle_of(schema):
        return True
    cle = schema.get("key")
    if isinstance(cle, str) and cle:
        return True
    haut = dsv2._fields(schema)
    return (bool(colonnes_a_etats(schema))
            or any(f.get("type") in TYPES_ARMES for f in haut)
            or any(dsv2.required_layers_of(f) for f in dsv2._walk_fields(haut)))


def releve(ns_id: int, schema: Optional[dict]) -> dict:
    """`{existing_violations, existing_violations_scope}`, ou `{}` quand il n'y a rien à
    juger (rien d'armé, tableau vide) — l'absence dit « pas examiné », un
    `existing_violations` vide avec `complete: true` dit « examiné, rien ».

    Par chemin : `rows` (lignes fautives), `blocking_rows` (celles qui n'acceptent plus
    aucune écriture, présent quand il y en a), `sample_ids` et `consequence`."""
    if not arme(schema):
        return {}
    total = db.datastore_count_rows(ns_id)
    if not total:
        return {}
    par_chemin: dict[str, dict[str, Any]] = {}
    examinees, apres = 0, None
    while examinees < PLAFOND_LIGNES:
        page = db.datastore_rows_data_after(
            ns_id, after_row_id=apres, limit=min(PAGE, PLAFOND_LIGNES - examinees))
        if not page:
            break
        for ligne in page:
            for chemin, nature in juger_ligne(schema, ligne["data"]).items():
                acc = par_chemin.setdefault(chemin, {"ids": [], "natures": {}})
                acc["natures"][nature] = acc["natures"].get(nature, 0) + 1
                if len(acc["ids"]) < ECHANTILLON:
                    acc["ids"].append(ligne["row_id"])
        examinees += len(page)
        apres = page[-1]["row_id"]
    violations = {}
    for chemin, acc in sorted(par_chemin.items(),
                              key=lambda kv: -sum(kv[1]["natures"].values())):
        entree: dict[str, Any] = {"rows": sum(acc["natures"].values())}
        if acc["natures"].get(BLOQUANTE):
            entree["blocking_rows"] = acc["natures"][BLOQUANTE]
        entree["sample_ids"] = acc["ids"]
        entree["consequence"] = _consequence(acc["natures"])
        violations[chemin] = entree
    return {"existing_violations": violations,
            "existing_violations_scope": {"rows_examined": examinees,
                                          "rows_total": max(total, examinees),
                                          "complete": examinees >= total}}


def releve_warning(out: dict) -> Optional[str]:
    """La phrase qui renvoie au relevé structuré — dite seulement quand il y a quelque
    chose à y lire, ou quand il est PARTIEL (un plancher ne se présente pas en total)."""
    violations = out.get("existing_violations") or {}
    portee = out.get("existing_violations_scope") or {}
    phrases = []
    if violations:
        detail = ", ".join(
            f"`{c}` ({v['rows']}" + (f", dont {v['blocking_rows']} bloquée(s)"
                                     if v.get("blocking_rows") else "") + ")"
            for c, v in violations.items())
        phrases.append(f"lignes existantes non conformes au schéma posé — {detail}. "
                       "Chemin par chemin, identifiants et conséquence dans "
                       "`existing_violations` : corrige-les avant d'armer, ou "
                       "assouplis la déclaration.")
    if portee and not portee.get("complete"):
        phrases.append(f"⚠️ relevé PARTIEL : {portee['rows_examined']} ligne(s) "
                       f"examinée(s) sur {portee['rows_total']} — les comptes de "
                       "`existing_violations` sont des PLANCHERS, pas des totaux.")
    return "\n".join(phrases) or None

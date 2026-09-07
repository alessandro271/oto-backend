"""Ce que ce tableau DÉCLARE et que la plateforme n'applique pas (#319).

Trois faits qu'un schéma laisse croire et que le moteur ne tient pas — dits au moment
où ils comptent, à la pose comme à l'écriture, jamais six semaines plus tard :

- **`options` hors régime strict ne contraint rien.** `validation_active` ne s'arme
  que sur `strict`/`required`/`required_when`/`max_length` : un tableau qui déclare
  une liste de choix et rien d'autre accepte tout. `options_not_enforced` le dit à la
  pose, `unenforced_options` nomme la valeur hors liste à l'écriture ;
- **un champ `type: json` n'est pas interrogeable en profondeur** — stocké et rendu
  tel quel, ni filtrable ni agrégeable au-delà du premier niveau (`json_fields_depth`).

⚠️ **On AVERTIT, on ne refuse pas.** Un tableau non-strict est en régime souple PAR
DÉCLARATION : y refuser changerait son contrat rétroactivement, et transformerait du
jour au lendemain des écritures qui passaient en erreurs, sans que personne l'ait
demandé.

⚠️ **Tout est DÉRIVÉ des fonctions qui décident** (`validation_active`,
`top_level_enum_options`, `lifecycle_of`), jamais d'une copie de leur logique : le jour
où `options` entrera dans `validation_active`, ces avertissements s'éteindront d'
eux-mêmes. Ce module existe précisément parce qu'une liste avait divergé du code.

Ce qu'il ne tient pas :
- **les clés que la plateforme ne lit pas du tout** → `vocabulaire.py` : ici, la clé
  est lue, elle est simplement sans effet dans ce régime ;
- **l'armement de la validation** lui-même → `declaration.validation_active` ;
- **le refus** d'une valeur hors options en régime strict → `validation.py`.
"""
from __future__ import annotations

from typing import Optional

from .declaration import (
    _fields,
    status_field,
    top_level_enum_options,
    validation_active,
    _walk_fields,
)
from .cycle_de_vie import lifecycle_of

# ── Options déclarées mais non appliquées (#319) ─────────────────────────────
#
# `validation_active` ne s'arme que sur `strict` / `required` / `required_when` /
# `max_length` — **`options` n'y est pas**. Un tableau qui déclare
# `options: ["oui","non","inconnu"]` et rien d'autre accepte « Peut-être » sans un mot.
#
# Le défaut a été signalé sur pièce par une mission, et il est aggravé par #316 : cet
# avertissement-là dirige vers `options` (« si tu voulais contraindre les valeurs, la
# clé est `options` ») — donc vers une clé qui, hors strict, ne contraint rien. Le
# correctif précédent avait déplacé le mensonge d'un cran.
#
# ⚠️ **On AVERTIT, on ne refuse pas.** Un tableau non-strict est en régime souple PAR
# DÉCLARATION : y refuser changerait son contrat rétroactivement. Mesuré en production
# le 13/08 — 23 tableaux sur 57 sont dans ce cas, et les 118 valeurs réellement hors
# liste sont TOUTES sur un seul, dont les écritures deviendraient des erreurs du jour
# au lendemain sans qu'il ait rien demandé. Le régime strict, lui, refuse déjà.
#
# ⚠️ **Tout est DÉRIVÉ des fonctions qui décident** (`validation_active`,
# `top_level_enum_options`), jamais d'une copie de leur logique : le jour où `options`
# entrera dans `validation_active`, ces avertissements s'éteindront d'eux-mêmes. Ce
# lot existe précisément parce qu'une liste avait divergé de ce que le code lit.


def _options_already_enforced(schema: Optional[dict]) -> set:
    """Les champs dont les valeurs sont DÉJÀ contraintes autrement que par `options`.

    ⚠️ Aujourd'hui il n'y en a qu'un : le champ `role="status"` porteur d'un
    `lifecycle`, dont les états sont refusés hors liste MÊME quand `validation_active`
    est faux (vérifié : un état inconnu lève, sans `strict`). L'avertir serait un FAUX
    POSITIF — et un avertissement qui crie à tort est celui qu'on apprend à ignorer,
    donc celui qui ruine les deux autres.

    Dérivé de `lifecycle_of`/`status_field`, jamais d'un nom en dur : le mécanisme de
    cycle de vie est en cours de retrait (#317) et cette exclusion s'éteindra d'
    elle-même le jour où il partira."""
    if lifecycle_of(schema) is None:
        return set()
    sf = status_field(schema) or {}
    key = sf.get("key")
    return {str(key)} if key else set()


def unenforced_options(schema: Optional[dict], data: dict) -> dict:
    """`{champ: valeur hors liste}` — et SEULEMENT quand rien ne les fait respecter.

    Vide dès que la validation est armée : là, une valeur hors options est REFUSÉE, et
    signaler en plus serait un doublon bavard sur un chemin qui ne peut pas passer.
    """
    if validation_active(schema) or not isinstance(data, dict):
        return {}
    deja = _options_already_enforced(schema)
    out: dict = {}
    for champ, opts in top_level_enum_options(schema).items():
        if champ in deja:
            continue
        v = data.get(champ)
        if v is not None and str(v) not in opts:
            out[champ] = str(v)
    return out


def unenforced_options_warning(hors: dict) -> Optional[str]:
    """La phrase qui accompagne le relevé — elle dit la CONSÉQUENCE avant le remède.

    Sans ça on lit « valeur inhabituelle » là où il faut lire « ce champ n'est pas la
    liste fermée que le schéma laisse croire »."""
    if not hors:
        return None
    detail = ", ".join(f"`{k}` = {v!r}" for k, v in sorted(hors.items()))
    return (f"valeur hors des options déclarées : {detail} — elle est ÉCRITE quand "
            "même. Ce tableau n'étant pas en format strict, les `options` de son "
            "schéma décrivent des choix proposés, elles ne les imposent pas. Pour "
            "qu'elles contraignent vraiment, pose `strict: true` sur le tableau "
            "(`data_set_schema`) — les écritures hors liste seront alors refusées.")


def options_not_enforced(schema: Optional[dict]) -> list[str]:
    """Les champs dont les `options` sont déclarées mais inertes — à la POSE.

    Pendant de #316, au moment qui compte : quand on écrit le schéma, pas six semaines
    plus tard en constatant les valeurs libres."""
    if validation_active(schema):
        return []
    deja = _options_already_enforced(schema)
    return sorted(c for c in top_level_enum_options(schema) if c not in deja)


def options_not_enforced_warning(champs: list[str]) -> Optional[str]:
    if not champs:
        return None
    noms = ", ".join(f"`{c}`" for c in champs)
    return (f"options déclarées mais NON appliquées : {noms} — ce tableau n'est pas "
            "en format strict, donc ces listes sont indicatives : une valeur hors "
            "liste sera acceptée. Ajoute `strict: true` au schéma pour qu'elles "
            "contraignent.")


def json_fields_depth(schema: Optional[dict]) -> list[str]:
    """Les champs `type: json` — dont le contenu n'est pas interrogeable en profondeur.

    Le fait est documenté, mais invisible AU MOMENT où on déclare le champ : une
    mission y a mis toute sa traçabilité par champ avant de découvrir qu'elle n'était
    ni filtrable ni agrégeable."""
    return sorted(str(f.get("key")) for f in _walk_fields(_fields(schema))
                  if f.get("type") == "json" and f.get("key"))


def json_depth_warning(champs: list[str]) -> Optional[str]:
    """⚠️ Énonce le FAIT, sans prescrire de contournement : la provenance native est
    en cours de conception, et recommander une structure aujourd'hui reviendrait à
    conseiller ce qui sera obsolète demain."""
    if not champs:
        return None
    noms = ", ".join(f"`{c}`" for c in champs)
    return (f"champ(s) `json` : {noms} — leur contenu est stocké et rendu tel quel, "
            "mais il n'est ni filtrable ni agrégeable au-delà du premier niveau : "
            "`data_rows` ne sait pas interroger une clé imbriquée, et l'export ne la "
            "déplie pas.")


# ── Un cycle de vie posé hors du champ de statut (07/09/2026) ────────────────
#
# Le troisième fait, et il coûte plus cher que les deux autres : `lifecycle_of` ne
# cherche le cycle de vie QUE sur le champ portant `role: "status"`. Un `lifecycle`
# déclaré sur n'importe quel autre champ est stocké, servi dans le schéma, et **jamais
# lu** — donc aucun état terminal, aucun plafond de reprises, aucun état d'abandon,
# aucun périmètre de réservation. La file tourne sans garde, et le schéma affiche le
# contraire.
#
# ⚠️ **La pose est déjà refusée** (`_validate_reserved_def` : « lifecycle exige
# role="status" »). Ça ne suffit pas, et c'est tout l'objet de ce relevé : **un schéma
# déjà en base ne se repose jamais.** Le refus ne parle qu'à celui qui écrit un schéma
# neuf ; celui qui a posé le sien avant la garde ne l'entendra jamais.
#
# Cas mesuré le 07/09/2026 sur un tableau de production d'une campagne vivante : la
# colonne d'état portait un `lifecycle` complet avec `role: "badge"`, pendant qu'une
# autre colonne portait `role: "status"` sans cycle de vie. Six semaines que la file
# de ce tableau n'était gardée par rien, sans un mot. Découvert par un tiers, en
# comparant un schéma avant et après un retrait d'attribut — pas par la plateforme.
#
# C'est le même incident que `required_layers` (posé, servi, sans lecteur) et que les
# clés non interprétées (#316) : une déclaration que le moteur ignore en silence est
# pire qu'une déclaration absente, parce que son auteur croit la garde armée.

def lifecycle_hors_statut(schema: Optional[dict]) -> list[str]:
    """Les champs portant un `lifecycle` que la plateforme ne lira jamais.

    DÉRIVÉ de `status_field`, jamais d'une copie de sa règle : le jour où le cycle de
    vie se lira ailleurs, ce relevé s'éteindra de lui-même."""
    ancre = status_field(schema)
    cle_ancre = str((ancre or {}).get("key") or "")
    return sorted(
        str(f.get("key")) for f in _walk_fields(_fields(schema))
        if isinstance(f.get("lifecycle"), dict) and f.get("key")
        and str(f.get("key")) != cle_ancre)


def lifecycle_hors_statut_warning(champs: list[str],
                                  schema: Optional[dict] = None) -> Optional[str]:
    """La phrase dit la CONSÉQUENCE avant la correction — sans quoi elle se lit comme
    un détail de style, alors qu'elle annonce une file de travail sans garde."""
    if not champs:
        return None
    noms = ", ".join(f"`{c}`" for c in champs)
    ancre = status_field(schema)
    cle = (ancre or {}).get("key")
    ou = (f"C'est `{cle}` qui porte `role: \"status\"` sur ce tableau, et "
          + ("il n'a pas de cycle de vie." if not lifecycle_of(schema)
             else "son cycle de vie est le seul que la file lit.")
          ) if cle else ("Aucun champ ne porte `role: \"status\"` sur ce tableau : "
                         "la file n'a donc aucune ancre.")
    return (f"cycle de vie NON LU : {noms} — oto ne lit le `lifecycle` que sur le "
            f"champ déclaré `role: \"status\"`. {ou} Tant que c'est le cas, ce "
            "tableau n'a AUCUN état terminal, AUCUN plafond de reprises, AUCUN état "
            "d'abandon et AUCUN périmètre de réservation : une ligne réservée n'est "
            "pas relâchée quand elle se termine, et la file peut tourner à vide "
            "indéfiniment. Déplace `role: \"status\"` sur la colonne qui porte le "
            "cycle de vie, ou déplace le cycle de vie sur la colonne qui porte le "
            "rôle — jamais pendant qu'une vague tourne.")

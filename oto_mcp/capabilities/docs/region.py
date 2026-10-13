"""Éditer UNE région d'un corps markdown sans le réémettre — la grammaire COMMUNE
d'`oto_doc op=patch` (une page) et d'`oto_guide op=patch` (un guide).

Une seule source pour la forme de l'appel (cible, mode, contenu), l'application du
moteur `doc_patch` et la traduction de ses refus : deux copies des mêmes phrases
divergeraient à la première évolution. Ce qui diffère d'un outil à l'autre tient
dans `Objet` — le nom de ce qu'on patche et le verbe qui le réécrit entier ; le
droit d'écrire, l'écriture elle-même et l'accusé restent à chaque outil.

**Deux axes d'adressage, jamais un mot réservé** (signaux #481, #492, #507) :
`section` est l'espace de noms des titres markdown ; `region` celui des régions SANS
titre (aujourd'hui le seul préambule). Les faire tenir dans un seul axe
(`section="__preamble__"`) rendrait inadressable tout corps portant réellement ce
titre — le jour où ça arrive, rien ne le signalerait.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional

from ... import doc_patch
from .._types import AuthzDenied

# La poignée du préambule, écrite UNE seule fois : elle sert dans le refus d'ambiguïté,
# dans le refus « cible manquante » et dans le refus « section introuvable » qui la
# pointe. Trois formulations divergeraient à la première évolution.
POIGNEE_PREAMBULE = 'region="preamble"'


@dataclass(frozen=True)
class Objet:
    """Comment les refus nomment ce qu'on patche — accord compris."""
    ce: str        # sujet d'une phrase : « Cette page »
    le: str        # complément : « la page »
    pronom: str    # « la supprimer la viderait »
    entier: str    # « la page ENTIÈRE »
    reecrire: str  # l'op qui réécrit le corps entier


PAGE = Objet("Cette page", "la page", "la", "ENTIÈRE", "op=update")
GUIDE = Objet("Ce guide", "le guide", "le", "ENTIER", "op=write")


def refus_section_introuvable(heading: str, disponibles: list, corps: str,
                              objet: Objet = PAGE) -> str:
    """Le message d'un `section=` qui ne résout pas : les sections DISPONIBLES, et —
    si le corps en a un — le rappel que le préambule existe et n'est pas une section.

    C'est le garde-fou du piège du mot réservé (signaux #481/#492/#507, qui
    proposaient `section="__preamble__"`) : cette forme-là ne devient JAMAIS un
    synonyme silencieux de la région — elle se heurte à ce refus, qui apprend la
    bonne poignée."""
    dispo = ", ".join(disponibles) or "(aucune)"
    msg = f"Section « {heading} » introuvable. Sections disponibles : {dispo}."
    if doc_patch.preamble(corps).strip():
        msg += (f" {objet.ce} a aussi un PRÉAMBULE (ce qui précède le premier titre : "
                f"bandeau de provenance, front-matter) : ce n'est pas une section, il "
                f"s'adresse par {POIGNEE_PREAMBULE}.")
    return msg


def refus_section_ambigue(heading: str, candidats: list, objet: Objet = PAGE) -> str:
    """Le message d'un `section=` qui désigne PLUSIEURS titres (#828, #869) : lesquels,
    avec leur niveau et leur ligne, et la seule sortie qui existe aujourd'hui. Sans la
    liste, l'agent ne peut que réessayer à l'identique ; sans la sortie, il réécrit le
    corps entier — ce que le patch existe pour éviter."""
    liste = " ; ".join(f"n°{k} `{'#' * niveau} {texte}` (ligne {ligne})"
                       for k, (ligne, niveau, texte) in enumerate(candidats, 1))
    return (f"Section « {heading} » ambiguë : {len(candidats)} titres y correspondent — "
            f"{liste}. `section` compare le TEXTE du titre, sans son niveau ni sa casse : "
            "aucun ne peut être visé seul, et RIEN n'a été modifié. Pour en viser un, "
            "rends d'abord les titres uniques — `mode=replace` sur une section PARENTE au "
            "titre unique qui les contient, en renommant l'un d'eux dans son `body_md`, "
            f"ou `{objet.reecrire}` avec le corps complet.")


def valider(section: Optional[str], region: Optional[str], mode: str,
            body_md: Optional[str]) -> bool:
    """La forme de l'appel, jugée AVANT toute lecture : UNE cible, et un contenu si et
    seulement si le mode en prend un. Rend `True` quand la cible est une section."""
    vise_section = bool(section and section.strip())
    # Les deux ensemble, ou aucun des deux : on refuse, on ne devine pas laquelle prime.
    if vise_section and region:
        raise AuthzDenied(
            400, "ambiguous_target",
            "`section` et `region` désignent deux cibles différentes : n'en passe "
            "qu'une. `section` = un titre markdown (la section court jusqu'au "
            f"prochain titre de niveau ≤) ; {POIGNEE_PREAMBULE} = ce qui précède le "
            "premier titre.")
    if not (vise_section or region):
        raise AuthzDenied(
            400, "missing_target",
            "Cible requise : `section` (le titre markdown de la section à modifier) "
            f"ou {POIGNEE_PREAMBULE} (ce qui précède le premier titre — bandeau de "
            "provenance, front-matter, tout ce qui n'appartient à aucune section).")
    # `mode=delete` retire la cible : il ne prend pas de contenu, et les trois autres
    # en exigent un. Refusé et NOMMÉ des deux côtés — un argument accepté-et-ignoré
    # coûte exactement ce qu'il prétendait économiser (leçon du signal #461).
    if mode == "delete":
        if body_md is not None:
            raise AuthzDenied(
                400, "unexpected_body",
                "`mode=delete` retire la cible : il ne prend pas de `body_md`. Pour "
                "VIDER une section en gardant son titre, c'est `mode=replace` avec "
                "`body_md` vide.")
    elif body_md is None:
        raise AuthzDenied(400, "missing_body", "`body_md` (nouveau contenu) requis.")
    return vise_section


class Patche(NamedTuple):
    corps: str               # le corps après le patch
    sous_sections: list      # les sous-sections qu'un replace/delete emporte
    emporte: Optional[list]  # les lignes retirées (`doc_patch.emprise`), None si rien


def appliquer(corps: str, section: Optional[str], body_md: Optional[str], mode: str,
              objet: Objet, affiche: Optional[str] = None) -> Patche:
    """Le patch appliqué à `corps`, ou un refus NOMMÉ. `section` None = le préambule.
    `affiche` = la section telle que l'appelant l'a écrite, pour les refus (défaut :
    `section`).

    Une section court jusqu'au prochain titre de niveau ≤ : un `replace` — ou un
    `delete` — sur un `###` emporte ses `####`. On ne refuse pas : on rend ce qui part
    (`sous_sections`), à l'outil de l'ANNONCER (signal #334). Calculé dans le même
    `try` que le patch : sur un titre AMBIGU, dire quels enfants partiraient est
    impossible, et le refus doit sortir par le même chemin (#869)."""
    nom = section if affiche is None else affiche
    try:
        enfants = (doc_patch.subsections(corps, section)
                   if section and mode in ("replace", "delete") else [])
        if section:
            nouveau = doc_patch.patch_section(corps, section, body_md, mode=mode)
        else:
            nouveau = doc_patch.patch_preamble(corps, body_md, mode=mode)
        # Après le patch, qui a déjà refusé une cible absente ou ambiguë.
        emporte = doc_patch.emprise(corps, section, mode)
    except doc_patch.SectionNotFound as e:
        raise AuthzDenied(404, "unknown_section", refus_section_introuvable(
            nom, e.available, corps, objet)) from None
    except doc_patch.AmbiguousSection as e:
        # Le premier titre venu était pris en silence : un replace visant le second
        # écrasait le premier, et l'accusé disait « ok » (#828, #869).
        raise AuthzDenied(409, "ambiguous_section",
                          refus_section_ambigue(nom, e.matches, objet)) from None
    except doc_patch.HeadingInPreamble as e:
        raise AuthzDenied(
            400, "heading_in_preamble",
            f"Le préambule est ce qui PRÉCÈDE le premier titre : y écrire un "
            f"titre ({', '.join(e.found)}) le refermerait, et le patch suivant "
            f"n'atteindrait plus ce bandeau. Retire le titre du `body_md`, ou "
            f"vise cette section par `section`.") from None
    except doc_patch.PreambleAbsent:
        raise AuthzDenied(
            404, "empty_preamble",
            f"{objet.ce} n'a pas de préambule : son premier titre ouvre le corps, "
            "il n'y a rien à supprimer au-dessus.") from None
    except doc_patch.PreambleIsWholePage:
        raise AuthzDenied(
            400, "preamble_is_whole_page",
            f"{objet.ce} n'a aucun titre : « ce qui précède le premier titre » y "
            f"est {objet.le} {objet.entier}, et {objet.pronom} supprimer "
            f"{objet.pronom} viderait. Si c'est ce que tu veux : {objet.reecrire} avec "
            f"le nouveau corps, ou op=delete pour {objet.le}.") from None
    return Patche(nouveau, enfants, emporte)

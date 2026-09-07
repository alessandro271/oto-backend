"""Le COÛT d'un motif — ce qui empêche un `pattern` de figer le serveur.

Un `pattern` de schéma est une expression régulière écrite par l'appelant et exécutée
par le serveur sur chaque écriture. Sans borne, `(a+)+$` sur une valeur assez longue
occupe un cœur indéfiniment : la garde n'est donc pas une question de goût, c'est la
condition pour accepter un motif venu du dehors.

Ce module ne fait que ça, et il le fait par MAJORATION statique : il déplie le motif
compilé (`re._parser`) et majore le nombre d'explorations qu'il peut coûter, sans
jamais l'exécuter. Trois bornes le tiennent — longueur du motif (`PATTERN_MAX_SRC`),
longueur du sujet qu'il peut viser (`PATTERN_MAX_SUBJECT`), budget d'exploration
(`PATTERN_BUDGET`) — et `pattern_refusal` rend la phrase de refus. `_pattern_re`
mémorise les motifs acceptés pour que le chemin chaud ne recompile pas.

**Il ne connaît rien du domaine** : ni schéma, ni colonne, ni couche. Il reçoit une
chaîne et une longueur maximale, il rend un refus ou `None`. C'est ce qui lui permet
d'être appelé à la fois par la LECTURE d'une déclaration (`declaration.pattern_of`)
et par la VALIDATION du schéma lui-même (`definition._validate_fields_def`) sans
créer de cycle entre elles.

Ce qu'il ne tient pas : **où** un motif est déclaré, **quand** il s'applique, et le
refus de la valeur qui ne le respecte pas → `declaration.py` et `validation.py`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

# ── `pattern` : la FORME d'une valeur, quand la taille ne suffit pas (#387) ───
#
# Jumeau de `max_length`, et il répond à ce que la borne ne sait PAS dire. Cas
# mesuré : un champ qui doit porter une énumération de catégories séparées par des
# points-virgules, pas une phrase de positionnement. Les longueurs des deux formes
# se recouvrent (20 à 207 caractères) — borner à 150 tue les deux, borner à 250
# n'attrape rien. Ce qui les sépare est la STRUCTURE.
#
# ⚠️ **Une expression fournie par un appelant est une arme.** Elle s'exécute à
# chaque écriture, DANS la boucle unique du serveur : un motif à explosion
# combinatoire n'y coûte pas une requête, il coûte le serveur entier — même famille
# que la bombe de décompression (docs/conventions.md, 13/08).
#
# Un garde purement SYNTAXIQUE (« pas de groupe quantifié ») ne suffit pas, et c'est
# une mesure, pas une intuition — sans un seul groupe ni une seule alternance :
#     `.*.*.*.*.*z`       sur  80 caractères ....   0,75 s
#     `.*.*.*.*.*.*.*z`   sur  60 caractères ....  14,8  s
# Ce qui explose est le nombre de FAÇONS de découper le sujet, pas la forme du motif.
#
# D'où un BUDGET calculé sur l'arbre du motif : le produit, quantificateur par
# quantificateur, du nombre de longueurs qu'il peut prendre — une majoration de
# l'espace de recherche du moteur. Il se calcule CONTRE la longueur du sujet, ce qui
# exige `max_length` sur le même champ : sans sujet borné il n'y a pas de budget,
# donc pas de garantie, et un motif dont on ne sait pas majorer le coût est refusé
# en le disant. Tout ce que l'analyse ne reconnaît pas est refusé de la même façon —
# fail-closed : un motif accepté par ignorance est exactement le défaut à éviter.

PATTERN_MAX_SRC = 200          # longueur du MOTIF
PATTERN_MAX_SUBJECT = 1000     # borne maximale d'un champ qui porte un motif
PATTERN_BUDGET = 100_000       # explorations majorées tolérées


def _re_parser():
    """Le parseur d'expressions de la stdlib — `re._parser` (3.11+) ou `sre_parse`
    (3.10, la version de la box). Aucun des deux : on ne sait plus majorer, donc on
    ne laisse plus passer un motif (le refus vit dans `pattern_refusal`)."""
    try:
        from re import _parser as p          # 3.11+
        return p
    except ImportError:
        pass
    try:
        import sre_parse as p                # 3.10
        return p
    except ImportError:                      # pragma: no cover — stdlib amputée
        return None


# Ce qu'on refuse en le NOMMANT, plutôt qu'en rendant « motif invalide » : chacune
# de ces constructions sort du modèle de coût, aucune n'a de majoration simple.
_PATTERN_REFUSES = {
    "GROUPREF": "une référence arrière",
    "GROUPREF_EXISTS": "un groupe conditionnel",
    "ASSERT": "une assertion avant/arrière",
    "ASSERT_NOT": "une assertion négative",
}

# Feuilles : elles consomment un caractère (ou zéro pour une ancre), sans choix.
_PATTERN_FEUILLES = {"LITERAL", "NOT_LITERAL", "IN", "ANY", "AT", "RANGE",
                     "CATEGORY", "NEGATE", "ANY_ALL"}


class _MotifTropCher(Exception):
    """Le motif sort du budget, ou de ce que l'analyse sait majorer."""


def _op_name(op) -> str:
    return getattr(op, "name", None) or str(op)


def _sub_budget(sub, cap: int) -> float:
    """Le budget d'une séquence — PRODUIT des budgets de ses termes.

    Le produit, pas la somme : le moteur revient en arrière, donc il explore le
    produit cartésien des découpages que ses termes autorisent. C'est exactement ce
    que la mesure de `.*.*.*z` montre, et c'est pourquoi une majoration additive
    laisserait passer la famille polynomiale."""
    total = 1.0
    for op, av in sub:
        total *= _node_budget(op, av, cap)
        if total > PATTERN_BUDGET:
            raise _MotifTropCher(
                f"il autorise plus de {PATTERN_BUDGET} découpages d'une valeur de "
                f"{cap} caractères")
    return total


def _node_budget(op, av, cap: int) -> float:
    nom = _op_name(op)
    if nom in _PATTERN_FEUILLES:
        return 1.0
    if nom in _PATTERN_REFUSES:
        raise _MotifTropCher(
            _PATTERN_REFUSES[nom] + " : cette construction n'a pas de coût "
            "majorable, oto ne l'exécute pas")
    if nom in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
        lo, hi, corps = av
        # `MAXREPEAT` (le sentinelle de `*`/`+`) vaut 2**32-1 : le sujet étant borné,
        # c'est la borne du champ qui plafonne le nombre de répétitions réelles.
        hi = cap if hi > cap else hi
        lo = cap if lo > cap else lo
        longueurs = float(max(hi - lo, 0) + 1)
        interne = _sub_budget(corps, cap)
        if interne > 1.0:
            # Un corps qui offre DÉJÀ un choix, répété : l'espace de recherche est
            # `interne ** répétitions`. C'est la famille `(a+)+` / `(a|aa)*`, celle
            # dont le coût est exponentiel — on ne la borne pas, on la refuse.
            raise _MotifTropCher(
                "un groupe qui offre déjà plusieurs découpages y est répété "
                "(`(a+)+`, `(a|aa)*`) — l'exploration y est exponentielle")
        return longueurs
    if nom == "SUBPATTERN":
        return _sub_budget(av[3], cap)
    if nom == "ATOMIC_GROUP":
        return _sub_budget(av, cap)
    if nom == "BRANCH":
        # Une alternance NON répétée coûte la somme de ses branches — `^(oui|non)$`
        # reste bon marché. Répétée, elle tombe dans le cas ci-dessus.
        return float(sum(_sub_budget(b, cap) for b in av[1])) or 1.0
    raise _MotifTropCher(
        f"il emploie une construction que l'analyse de coût ne reconnaît pas "
        f"({nom}) — oto n'exécute que ce dont elle sait majorer le prix")


def pattern_refusal(src: str, max_length: int) -> Optional[str]:
    """La RAISON de refuser ce motif sur un champ borné à `max_length`, ou None.

    Rendue en clair et adressée à l'auteur : un refus qui dit « motif invalide » ne
    laisse rien à corriger, et c'est ici — à la pose — qu'il reste corrigible."""
    if not isinstance(src, str) or not src:
        return "un motif est une chaîne non vide"
    if len(src) > PATTERN_MAX_SRC:
        return (f"{len(src)} caractères, maximum {PATTERN_MAX_SRC} — au-delà, le "
                "coût d'exécution n'est plus majorable de façon utile")
    try:
        re.compile(src)
    except re.error as e:
        return f"expression invalide ({e})"
    parseur = _re_parser()
    if parseur is None:                      # pragma: no cover
        return ("le parseur d'expressions de la stdlib est introuvable : oto ne "
                "peut pas majorer le coût de ce motif, donc ne l'exécute pas")
    try:
        arbre = parseur.parse(src)
    except Exception as e:                   # noqa: SILENT — traduit en refus nommé
        return f"expression illisible par l'analyse de coût ({e})"
    try:
        _sub_budget(arbre, int(max_length))
    except _MotifTropCher as e:
        return str(e)
    return None


@lru_cache(maxsize=256)
def _pattern_re(src: str):
    """Le motif compilé, mémorisé — il s'exécute à chaque écriture de ligne."""
    return re.compile(src)

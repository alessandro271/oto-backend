"""Le FILTRE d'un webhook : ce qu'une livraison doit porter pour lancer un agent.

Une source branchée une fois envoie TOUT ce qu'elle sait envoyer : une app Slack
abonnée aux messages reçoit chaque message de chaque canal où elle est, un CRM
abonné aux changements reçoit chaque changement. Sans filtre, le seul tri
possible est celui de l'agent — un run entier, modèle compris, pour conclure
« ce n'est pas pour moi ». Le filtre fait ce tri à la porte, sans run.

## La forme, volontairement petite

`{"<chemin>": [<valeur>, …], …}` — chaque chemin du corps doit valoir l'une des
valeurs listées, et TOUS les chemins doivent correspondre (ET entre les chemins,
OU dans une liste). Un chemin absent ne correspond à rien. Exemple, une app
Slack qui ne doit réagir qu'aux canaux partagés avec un client :
`{"is_ext_shared_channel": [true], "event.type": ["message"]}`.

Pas d'opérateur, pas d'expression : un langage d'expression appliqué à une
donnée hostile est une surface d'attaque, et `runner_hook._extraire` tient la
même ligne. Les chemins se lisent comme ceux de `payload_fields` (`a.b`, un
index numérique pour un tableau, `$.` initial toléré).

## Ce qu'il n'est pas

Ni une preuve ni une garde : il ne juge qu'une livraison DÉJÀ authentifiée, et
ce qu'il écarte rend un succès à la source (une source qui reçoit un refus
retente, et finit par désactiver le hook). Il ne remplace pas `max_per_day`.
"""
from __future__ import annotations

import json
from typing import Any, Optional

#: Bornes de POSE. Un filtre est une donnée de réglage, pas une liste de clients.
MAX_CHEMINS = 10
MAX_VALEURS = 50
MAX_CHEMIN = 200
MAX_VALEUR = 200

_ABSENT = object()


def _valeur(corps: Any, chemin: str) -> Any:
    """La valeur BRUTE d'un chemin — jamais convertie en texte : `true` et
    `"true"` ne sont pas la même chose, et un filtre qui les confondrait
    laisserait passer ce que son auteur a écarté."""
    courant = corps
    for cle in chemin.lstrip("$").strip(".").split("."):
        if not cle:
            continue
        if isinstance(courant, dict) and cle in courant:
            courant = courant[cle]
        elif isinstance(courant, list) and cle.isdigit() and int(cle) < len(courant):
            courant = courant[int(cle)]
        else:
            return _ABSENT
    return courant


def _egal(recue: Any, attendue: Any) -> bool:
    # ⚠️ `True == 1` en Python : sans le contrôle de type, `[1]` laisserait passer
    # `true`, et `[true]` laisserait passer `1`.
    if isinstance(recue, bool) or isinstance(attendue, bool):
        return isinstance(recue, bool) and isinstance(attendue, bool) and recue == attendue
    return recue == attendue


def correspond(filtre: Optional[dict], corps: Any) -> bool:
    """La livraison passe-t-elle le filtre ? Pas de filtre = tout passe."""
    if not filtre:
        return True
    for chemin, valeurs in filtre.items():
        recue = _valeur(corps, str(chemin))
        if recue is _ABSENT:
            return False
        if not any(_egal(recue, v) for v in (valeurs or ())):
            return False
    return True


def probleme(filtre: Any) -> Optional[str]:
    """Pourquoi ce filtre ne peut pas être posé, ou None s'il le peut.

    Refusé à la POSE plutôt qu'accepté inerte : un filtre mal formé qui ne
    correspond jamais à rien éteindrait l'agent sans que personne ne le voie."""
    if not isinstance(filtre, dict):
        return "le filtre est un objet `{chemin: [valeurs]}`"
    if len(filtre) > MAX_CHEMINS:
        return f"au plus {MAX_CHEMINS} chemins"
    for chemin, valeurs in filtre.items():
        if not isinstance(chemin, str) or not chemin.lstrip("$").strip(".") \
                or len(chemin) > MAX_CHEMIN:
            return f"chemin invalide : {chemin!r}"
        if not isinstance(valeurs, list) or not valeurs:
            return (f"`{chemin}` : une LISTE non vide de valeurs acceptées "
                    "(une liste vide ne laisserait jamais rien passer)")
        if len(valeurs) > MAX_VALEURS:
            return f"`{chemin}` : au plus {MAX_VALEURS} valeurs"
        for v in valeurs:
            if not (v is None or isinstance(v, (str, int, float, bool))):
                return (f"`{chemin}` : des valeurs simples seulement (texte, nombre, "
                        "booléen, null) — un objet ne se compare pas")
            if isinstance(v, str) and len(v) > MAX_VALEUR:
                return f"`{chemin}` : une valeur dépasse {MAX_VALEUR} caractères"
    if len(json.dumps(filtre, ensure_ascii=False)) > 8_192:
        return "le filtre dépasse 8 Ko"
    return None

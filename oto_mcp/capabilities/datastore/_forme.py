"""La forme des cellules à couches, partagée par les lectures et la réservation.

⚠️ **Ce module existe pour ne pas réordonner la table de routes.** `claim.py` a besoin
du même champ que `rows.py` ; l'importer depuis `rows` chargerait `rows` en premier et
changerait l'ordre d'enregistrement des capacités — or cette table est FIGÉE, parce que
Starlette sert le PREMIER chemin qui matche. Un tiers neutre, importé par les deux, ne
touche à rien.

Une seule définition, donc : même texte servi, même refus nommé sur une valeur inconnue,
des deux côtés. Deux copies divergeraient le jour où le défaut de `layers` basculera.
"""
from __future__ import annotations

from pydantic import Field

from ...datastore import layers as dsl
from ...datastore import versions as dsver
from .._types import AuthzDenied

# oto#53 : typé `str` et non `Literal` pour que la mauvaise valeur rende un refus qui
# NOMME le paramètre (`invalid_layers`), pas l'`invalid_input` nu que l'adaptateur rend
# sur une `ValidationError`.
_LAYERS = Field(default=dsl.DEFAUT, description=(
    "Forme des cellules à couches. ⚠️ C'est une LECTURE : ne renvoyez pas la ligne "
    "lue. N'écrivez que ce que vous avez établi — et jamais `champ.origine`, qui se "
    "lit ici et que pose la plateforme, jamais un agent. On ÉCRIT imbriqué (`champ` = "
    "`{valeur, comment, link}`) et, par défaut, on relit À PLAT : ce paramètre lève cette "
    "asymétrie. `flat` (défaut) sert `champ` = la valeur et `champ.origine`/`.comment`/"
    "`.link` à plat à côté ; `nested` sert `champ` = `{valeur, origine, comment, link}` "
    "(la valeur toujours, les couches renseignées seulement), la forme dans laquelle on "
    "écrit ; une cellule sans couche est le même scalaire dans les deux. Toute autre "
    "valeur est refusée. Le défaut basculera vers `nested`, avec préavis daté : un "
    "client qui dépend d'une forme la nomme dès maintenant."))


def _layers(raw) -> str:
    """`?layers=` validé, ou un 400 qui nomme le paramètre et les valeurs admises."""
    try:
        return dsl.check(raw)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_layers", str(e))


# oto#140 : même raison de vivre ici que `_LAYERS` — la réservation lit les mêmes
# cellules que la page, et deux définitions divergeraient le jour où le défaut bascule.
# Typé `list[str] | None` plutôt qu'un `Literal` : une valeur inconnue doit rendre un
# refus qui NOMME le paramètre et ce qui est admis, pas un `invalid_input` nu.
_VERSIONS = Field(default=None, description=(
    "Quelles VERSIONS de chaque case servir. `current` = ce qu'on a établi, `origine` "
    "= ce que la cliente a remis à l'import. ⚠️ Demandez les DEUX dans le MÊME appel "
    "quand vous les comparez : deux appels ne sont pas atomiques, et une écriture "
    "entre les deux ferait comparer l'avant d'un état à l'après d'un autre — un écran "
    "annoncerait « corrigé » sur une ligne que personne n'a touchée. Le nom NU porte "
    "TOUJOURS la version courante ; ce paramètre décide seulement de ce qui s'ajoute "
    "à côté (`champ.origine` et ses sous-champs). La réponse déclare ce qu'elle a "
    "servi dans `versions_servies`, pour que « je ne l'ai pas demandée » ne ressemble "
    "jamais à « cette case n'en a pas ». Le défaut sert encore les deux ; il "
    "basculera vers `current` seul, avec préavis daté."))


def _versions(raw) -> tuple:
    """`?versions=` validé, ou un 400 qui nomme le paramètre et les versions admises."""
    try:
        return dsver.check(raw)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_versions", str(e))

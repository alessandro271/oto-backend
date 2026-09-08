"""`versions` — quelles VERSIONS d'une case la lecture rend (oto#140).

Une case existe en deux versions : `current`, ce qu'on a établi, et `origine`, ce que
la cliente a remis. Une écriture vise toujours la courante et n'a pas à la nommer ; une
lecture, elle, doit pouvoir dire ce qu'elle veut recevoir.

```
versions = ["current", "origine"]     # les deux, dans la même réponse
versions = ["current"]                # la cible du contrat
```

## Pourquoi les deux dans le MÊME appel, et pas deux appels

Demandé par le consommateur qui affiche l'écart « donné / produit », avec un argument
qui tranche : **deux appels ne sont pas atomiques**. Une écriture entre les deux ferait
comparer l'avant d'un état à l'après d'un autre — et l'écran annoncerait « corrigé » sur
une ligne que personne n'a touchée. S'y ajoute le coût banal : une jointure par `_id`
côté navigateur sur des pages de 500 lignes, et deux fois le réseau pour une question.

## Ce que la réponse DÉCLARE

⚠️ La réponse porte les versions servies (`versions_servies`), au niveau de la réponse
et jamais de la cellule. C'est ce qui rend discernables **« je ne l'ai pas demandée »**
et **« elle n'existe pas sur cette case »** — sans quoi le lecteur réinventerait un
marqueur, en pire, puisque cette fois il l'aurait deviné. Ça ne coûte rien par cellule
et la réponse devient autoportante : elle dit ce qu'elle contient.

## Palier 1 d'une bascule en trois temps

Le défaut REND ENCORE LES DEUX, donc rien ne change pour personne aujourd'hui. Le
contrat prévoit qu'il serve `current` seul — ce qui fera disparaître `champ.origine`
des lectures qui ne le demandent pas.

⚠️ **Cette bascule n'est pas une économie de données, elle supprime la surface d'un
incident.** Un écran avait comparé la valeur de départ à la valeur courante et
s'apprêtait à annoncer à une cliente « nous avons corrigé votre valeur » en lui
montrant du texte écrit par la plateforme. Ce genre d'accident demande que la donnée
soit là **sans qu'on l'ait demandée** ; qui la demande explicitement sait ce qu'il
affiche. Le consommateur concerné l'a reconnu lui-même : sa garde protège d'un marqueur
connu, la demande explicite protège de ce que personne n'avait prévu.

**Le défaut se lit ici et nulle part ailleurs** — les deux faces le prennent d'ici, pour
qu'une bascule soit un seul geste. Même règle que `layers.DEFAUT`, et pour la même
raison : un défaut recopié diverge le jour où on le change.
"""
from __future__ import annotations

from typing import Any, Optional

CURRENT = "current"
ORIGINE = "origine"
VERSIONS = (CURRENT, ORIGINE)

#: ⚠️ Palier 3 : bascule vers `(CURRENT,)`, avec préavis daté et 24 h d'annonce au
#: consommateur qui lit la version de départ — il l'a demandé, et il a raison : ce
#: n'est pas la longueur du travail qui commande, c'est de pouvoir le vérifier à
#: l'écran un jour où ce n'est pas la veille d'une revue cliente.
DEFAUT = (CURRENT, ORIGINE)


def check(value: Any) -> tuple:
    """La valeur de `versions`, ou un refus qui NOMME le paramètre, ce qui a été reçu
    et ce qui est admis — jamais un `invalid_input` nu qui oblige à deviner.

    `None` vaut le défaut : les deux faces passent leur paramètre tel quel, et une face
    qui n'a rien reçu ne doit pas avoir à connaître le défaut de l'autre.

    ⚠️ Une liste VIDE est refusée, pas traitée comme « le défaut ». `versions=[]` est un
    geste délibéré qui ne veut rien dire — le lire comme un défaut servirait exactement
    l'inverse de ce qu'il demande, et sans un mot.
    """
    if value is None:
        return DEFAUT
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(
            f"`versions` attend une liste non vide parmi {list(VERSIONS)} — reçu "
            f"{value!r}. Une case existe en deux versions : `{CURRENT}` (ce qu'on a "
            f"établi) et `{ORIGINE}` (ce que la cliente a remis). Pour comparer les "
            f"deux, demande-les dans le MÊME appel : deux appels ne sont pas "
            f"atomiques.")
    inconnues = [v for v in value if v not in VERSIONS]
    if inconnues:
        raise ValueError(
            f"`versions` : {inconnues} n'existe pas. Les versions sont "
            f"{list(VERSIONS)} — `{CURRENT}` est la valeur courante, `{ORIGINE}` celle "
            f"que la cliente a remise à l'import.")
    # Ordonnées comme VERSIONS, dédoublonnées : la réponse déclare ce qu'elle sert, et
    # deux appelants qui demandent la même chose dans un ordre différent doivent lire
    # la même déclaration.
    return tuple(v for v in VERSIONS if v in value)


def sert_l_origine(versions: Optional[tuple]) -> bool:
    """La version d'origine fait-elle partie de ce qui est demandé ?"""
    return ORIGINE in (versions or DEFAUT)

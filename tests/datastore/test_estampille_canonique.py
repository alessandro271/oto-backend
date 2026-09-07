"""Les estampilles posées par la PLATEFORME ont une seule forme (#859).

Alexis, en relisant le lot sur le tri : *« cette date est déterministe, elle
n'est pas renseignée par l'agent, c'est pas un champ writable »*. C'est exact —
et c'est justement ce qui rend le défaut gênant : **c'est nous qui écrivions
plusieurs formes.**

Mesuré le 03/09 : `2026-09-03T11:22:19+00:00` (seconde) à côté de
`2026-09-03T11:22:19.619406+00:00` (microseconde), et un troisième format vu en
production (`…T00:00:00.000Z`) qui précède ce cran.

⚠️ **Le tri est ce qui paie** : `Z` et `+00:00` désignent le même décalage et ne
se rangent pas pareil dans l'alphabet. Le tri caste désormais en horodatage, donc
il absorbe l'existant — mais *corriger la lecture d'une donnée qu'on écrit
soi-même de travers, c'est réparer autour de la source*. Ce banc garde la source :
`iso_utc`, par où passe tout ce que la plateforme date (`core._now_iso`).

⚠️ Une des sources de ce banc a disparu avec le cran de valeur `system:` (retiré
le 07/09/2026, zéro colonne en production) : le test qui comparait les DEUX
sources entre elles est parti avec elle. Ce qui reste garde la forme elle-même.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oto_mcp.datastore.reserves import iso_utc

_PARIS = timezone(timedelta(hours=2))
_INSTANT = datetime(2026, 9, 3, 13, 22, 19, 619406, tzinfo=_PARIS)


def test_un_decalage_est_ramene_en_UTC():
    """13 h 22 à Paris, c'est 11 h 22 UTC. Sans ce ramené, deux estampilles du
    même instant portent deux heures différentes selon le fuseau de la source."""
    assert iso_utc(_INSTANT) == "2026-09-03T11:22:19+00:00"


def test_la_fraction_de_seconde_est_ECARTEE():
    """On date un travail, pas une mesure physique. La précision perdue n'a aucun
    usage ; l'uniformité, elle, se voit à chaque tri."""
    assert "." not in iso_utc(_INSTANT)


def test_la_notation_du_decalage_est_UNE():
    """`Z` et `+00:00` sont le même décalage et se rangent différemment dans
    l'alphabet — c'est exactement ce qui inversait deux instants identiques."""
    rendu = iso_utc(_INSTANT)
    assert rendu.endswith("+00:00") and not rendu.endswith("Z")


def test_une_valeur_qui_n_est_PAS_un_instant_traverse_sans_etre_inventee():
    """Une source qui rendrait autre chose qu'une date ne doit pas se voir
    fabriquer un horodatage plausible : un champ vide se voit, une valeur fausse
    se croit."""
    assert iso_utc("pas une date") == "pas une date"
    assert iso_utc(None) == "None"

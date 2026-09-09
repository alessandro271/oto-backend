"""Le texte SERVI ne promet plus `origine: "system"`, supprimé le 08/09/2026.

Le cran armait une capture automatique : à la première écriture qui changeait une
valeur, la plateforme figeait la précédente comme origine. Il a été **supprimé**
(`declaration.system_origin_fields` rend toujours `set()`) et remplacé par
`donnees_d_origine`, un geste DÉCLARÉ qui pose la version d'origine au moment où la
valeur entre.

⚠️ **La description de `data_write` a continué de le promettre pendant un jour**, et
c'est le texte le plus lu de la plateforme. Trois affirmations, toutes fausses, sur la
garantie que le produit met en avant :

- « The safety net is `origine: "system"` » — le filet n'existe plus ;
- « writing `<field>.origine` is REFUSED, **today** » — `refus_arme()` est `False`, le
  refus est daté du 01/10/2026 ;
- « **no parameter lifts that** » — `origine_override` existe, et il est offert dans le
  schéma du même outil.

Un agent qui lisait ça renonçait à un geste permis, ou comptait sur un filet absent.
C'est la classe « le texte servi pilote l'agent » : une description d'outil est du code
de production.

**Ce banc n'interdit pas de NOMMER le cran** — il faut pouvoir dire qu'il est parti,
sinon le trafic qui l'emploie encore ne reçoit aucune explication. Il interdit de le
présenter comme disponible : toute mention doit porter son retrait.
"""
from __future__ import annotations

import inspect
import re

from oto_mcp.datastore.declaration import system_origin_fields


def test_le_mecanisme_est_bien_MORT_avant_de_juger_le_texte():
    """⚠️ D'abord la mesure, ensuite le texte. Si le cran revenait un jour, c'est CE
    banc qui doit tomber en premier — sans quoi il interdirait de documenter un
    mécanisme redevenu vivant."""
    assert system_origin_fields({"fields": [{"key": "a", "origine": "system"}]}) == set()


def test_aucune_mention_SERVIE_ne_le_presente_comme_disponible():
    """Toute ligne du texte servi qui nomme le cran doit porter son retrait — dans la
    même phrase, pas trois paragraphes plus loin : un agent lit la ligne qu'il lit."""
    import oto_mcp.tools.datastore as T
    src = inspect.getsource(T)
    fautives = []
    for n, ligne in enumerate(src.splitlines(), 1):
        if 'origine: "system"' not in ligne:
            continue
        # la mention doit dire, sur place, que c'est parti
        if not re.search(r"REMOVED|removed|SUPPRIM|supprim", ligne):
            fautives.append(f"{n}: {ligne.strip()[:90]}")
    assert not fautives, (
        "ces lignes SERVIES nomment `origine: \"system\"` sans dire qu'il est "
        f"supprimé — un agent les lira comme une option disponible : {fautives}")


def test_les_trois_affirmations_fausses_ont_disparu():
    """Les phrases exactes qui ont menti, nommées pour qu'on ne les réintroduise pas
    par copier-coller depuis un ancien commit ou une doc."""
    import oto_mcp.tools.datastore as T
    src = inspect.getsource(T)
    for phrase in ("The safety net is `origine",
                   "is REFUSED, today",
                   "no parameter lifts that"):
        assert phrase not in src, f"phrase périmée réintroduite : {phrase!r}"


def test_le_REMPLACANT_est_nomme_là_où_le_filet_est_annoncé_absent():
    """⚠️ Dire « il n'y a plus de filet » sans dire ce qui le remplace laisse l'agent
    sans conduite — c'est la moitié d'un refus, celle qui ne sert à rien. Le geste qui
    aboutit doit être nommé au même endroit."""
    import oto_mcp.tools.datastore as T
    src = inspect.getsource(T)
    i = src.find("There is NO automatic safety net")
    assert i != -1, "l'avertissement d'absence de filet a disparu"
    voisinage = src[i:i + 1200]
    assert "donnees_d_origine" in voisinage, (
        "l'absence de filet est annoncée sans nommer le geste qui la remplace")


def test_la_date_du_refus_servie_est_celle_du_CODE():
    """Un préavis daté dans un texte servi et une date dans le code qui divergent, et
    c'est le texte qui aura tort le jour venu."""
    from oto_mcp.datastore import champs_reserves as cr
    import oto_mcp.tools.datastore as T
    attendue = cr.ORIGINE_REFUS_LE.isoformat()
    src = inspect.getsource(T)
    assert attendue in src, (
        f"le texte servi ne porte pas la date du code ({attendue}) — deux dates "
        f"divergeraient, et l'affichage aurait tort")

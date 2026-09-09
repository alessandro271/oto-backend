"""Le nombre rendu à la pose disait « capturées » et comptait des PERTES.

Racine d'une confusion qui est remontée jusqu'au propriétaire du produit le
08/09/2026 : il a affirmé de bonne foi que les valeurs d'origine étaient conservées
sur un tableau de production — **parce que c'est exactement ce que la plateforme
promettait**. La docstring de la fonction qui balise annonçait « pose l'origine = la
valeur COURANTE », et la clé servie s'appelle `origines_capturees`.

Le code, lui, écrit le marqueur « origine inconnue » depuis oto#70. Le commentaire de
la requête l'explique en détail, dix lignes plus bas que la docstring qui le contredit.

⚠️ **Un texte périmé ne se contente pas d'être inutile — il fabrique une croyance, et
il la fabrique chez ceux qui prennent la peine de lire.** Ici : 837 marqueurs sur 846
couches, une restitution muette devant une cliente, et une découverte trois semaines
après la pose.

Ce banc fige les deux moitiés : que la phrase DISE la perte, et qu'elle dise **l'ordre
de gestes qui l'évite** — parce que ce n'est pas un défaut, c'est une inversion.
"""
from __future__ import annotations

import inspect

from oto_mcp.datastore import schema as dsv2
from oto_mcp.db import datastore as dsdb


def test_la_phrase_dit_une_PERTE_pas_une_capture():
    """Le mot compte : `origines_capturees` se lit comme un succès."""
    phrase = dsv2.marqueurs_poses_warning(837)

    assert "837" in phrase
    assert "PERTE" in phrase and "pas une capture" in phrase
    assert "ne se reconstituera pas" in phrase, (
        "l'irréversibilité doit être dite : c'est ce qui change la réaction du lecteur")


def test_la_phrase_dit_LE_GESTE_qui_l_evite():
    """Sans ça, elle annonce un dégât sans issue — et un avertissement sans geste se
    lit comme une fatalité.

    ⚠️ Le geste a CHANGÉ le 08/09/2026, l'exigence non. L'ancien conseil était un
    ORDRE à respecter (déclarer le cran avant d'importer) ; le cran est supprimé, et
    ce qui le remplace ne dépend d'aucun ordre — `donnees_d_origine` pose l'origine
    au moment où la valeur entre. Ce banc garde l'exigence, pas la formulation."""
    phrase = dsv2.marqueurs_poses_warning(12)

    assert "donnees_d_origine" in phrase, "un avertissement sans geste est une fatalité"
    assert "SUPPRIMÉ" in phrase, "l'ancien conseil doit être désigné comme périmé"


def test_elle_se_TAIT_quand_rien_n_a_ete_marque():
    """Le cas normal — une déclaration sur un tableau vide ne balise rien, et ne doit
    pas produire de clé parasite dans la réponse."""
    assert dsv2.marqueurs_poses_warning(0) is None


def test_la_docstring_ne_promet_plus_la_valeur_courante():
    """⚠️ La moitié qu'aucun banc ne garde d'habitude, et c'est elle qui a menti.

    Le code était juste et documenté juste — dans le commentaire de la requête. C'est
    la DOCSTRING, lue en premier par tout le monde, qui décrivait le comportement
    retiré. Un banc sur le comportement seul n'aurait rien vu."""
    doc = inspect.getdoc(dsdb.datastore_capturer_origine)

    assert "MARQUEUR" in doc, "la docstring doit dire ce que la fonction écrit"
    assert "la valeur COURANTE sur les lignes existantes" not in doc, (
        "l'ancienne promesse ne doit pas subsister")
    assert "AVANT" in doc, "et dire l'ordre qui capture vraiment"


# ── la moitié SERVIE : ce que l'agent lit avant d'importer ────────────────────
# Les deux bancs ci-dessus gardent des textes INTERNES — une docstring, une phrase de
# réponse. Le texte ci-dessous est servi aux agents sur la porte de l'IMPORT, donc à
# l'endroit exact où l'ordre des gestes se décide. Il portait la MÊME promesse, sans
# sa condition : « l'origine est conservée et posée par la plateforme quand elle
# manque ». Vraie dans un sens de l'ordre, fausse dans l'autre — et c'est l'autre qui
# a mordu.

def test_la_description_servie_ne_promet_RIEN_d_inconditionnel():
    """⚠️ Une promesse sans sa condition n'est pas « imprécise », elle est FAUSSE dans
    le cas qu'elle omet. Et celui qui la lit n'ira pas vérifier : un texte servi est cru
    davantage qu'une mesure, parce qu'il émane de la chose même qu'il décrit."""
    for en in (False, True):
        texte = dsv2.description_parametre_origine(en=en)
        assert "l'origine est conservée" not in texte
        assert "the origin is kept" not in texte


def test_la_description_servie_cite_le_MARQUEUR_litteralement():
    """⚠️ **L'exigence qui survit au retrait du mécanisme**, et j'ai failli la perdre
    en corrigeant les textes : le marqueur est cité LITTÉRALEMENT sur les deux faces,
    l'anglaise comprise. Un agent qui cherchera cette chaîne dans ses données doit
    trouver celle qui y est écrite, pas sa traduction.

    ⚠️ La CONDITION, elle, a disparu avec le cran (`origine: "system"`, supprimé le
    08/09/2026) : l'exiger encore ferait garder une phrase fausse. Ce qui reste vrai,
    c'est que le marqueur est TOUJOURS EN BASE sur les lignes que le mécanisme n'a pas
    su reconstituer — donc toujours rencontrable, donc toujours à expliquer."""
    from oto_mcp.datastore.couches import ORIGINE_INCONNUE
    fr = dsv2.description_parametre_origine()
    en = dsv2.description_parametre_origine(en=True)

    assert ORIGINE_INCONNUE in fr and ORIGINE_INCONNUE in en, (
        "le marqueur doit rester citable tel qu'il est écrit dans les données")
    # et il est présenté pour ce qu'il est
    assert "PERTE" in fr and "LOSS" in en
    # le remplaçant est nommé sur les deux faces
    assert "donnees_d_origine" in fr and "donnees_d_origine" in en

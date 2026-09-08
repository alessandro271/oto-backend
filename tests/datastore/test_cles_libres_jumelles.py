"""Deux orthographes libres d'une même clé, dans un même schéma.

Cas mesuré le 08/09/2026, apporté par un consommateur qui préparait une revue client :

    raison_sociale   proprietaire     ← la clé voulue
    effectif         propietaire      ← une coquille

L'avertissement des clés non interprétées les rendait **côte à côte dans la même
phrase**, `near_miss` vide pour les deux — parce qu'aucune n'a de cousine dans le
vocabulaire d'oto. Rien ne distinguait la clé délibérée de la faute de frappe, et la
colonne mal étiquetée serait tombée silencieusement hors du regroupement d'un écran
client : pas en erreur, ABSENTE, avec un décompte qui garde l'air juste.

⚠️ Ce banc garde autant le SEUIL que la détection. Un signal de qualité qui crie sur
des schémas sains s'use, et ne sert plus le jour où il a raison (doctrine payée sur
`label`, cf. `cles_inconnues`). Les cas négatifs ci-dessous valent donc autant que les
positifs — ils sont même les plus faciles à casser en « améliorant » le seuil.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2


# ── le seuil, dans les deux sens ──────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("proprietaire", "propietaire"),    # suppression — le cas réel, mesuré
    ("proprietaire", "proprietaires"),  # insertion finale
    ("proprietaire", "propprietaire"),  # doublement d'une lettre
])
def test_le_seuil_retient_l_insertion_et_la_suppression(a, b):
    assert dsv2.sont_jumelles(a, b)
    assert dsv2.sont_jumelles(b, a), "la relation doit être symétrique"


def test_la_transposition_est_retenue():
    """`ai` → `ia` : personne ne choisit ça exprès."""
    assert dsv2.sont_jumelles("proprietaire", "proprietiare")


@pytest.mark.parametrize("a, b", [
    ("label_fr", "label_en"),          # substitution DÉLIBÉRÉE — deux langues
    ("seuil_min", "seuil_max"),        # substitution délibérée — deux bornes
    ("colonne_a", "colonne_b"),        # substitution délibérée — deux variantes
    ("proprietaire", "propriataire"),  # substitution FAUTIVE — sciemment manquée
])
def test_la_SUBSTITUTION_ne_declenche_PAS(a, b):
    """⚠️ La moitié du travail. La substitution est une faute de frappe courante, mais
    c'est AUSSI le moyen normal de distinguer deux clés voisines. La retenir ferait
    crier l'avertissement sur des schémas parfaitement sains — et un avertissement
    qu'on apprend à ignorer ne sert plus le jour où il a raison."""
    assert not dsv2.sont_jumelles(a, b)


@pytest.mark.parametrize("a, b", [("min", "man"), ("of", "off"), ("cle", "clef")])
def test_les_cles_COURTES_ne_declenchent_pas(a, b):
    """Sur des clés courtes, un caractère d'écart est le mode NORMAL de distinction."""
    assert not dsv2.sont_jumelles(a, b)


def test_une_cle_n_est_pas_sa_propre_jumelle():
    assert not dsv2.sont_jumelles("proprietaire", "proprietaire")


# ── le relevé : qui porte quoi, sans jamais désigner un coupable ──────────────

def _schema(*paires):
    return {"fields": [{"key": champ, cle: "x"} for champ, cle in paires]}


def test_le_releve_apparie_et_compte_les_porteuses():
    inconnues = dsv2.unknown_declaration_keys(_schema(
        ("raison_sociale", "proprietaire"),
        ("ca", "proprietaire"),
        ("effectif", "propietaire"),
    ))
    paires = dsv2.cles_libres_jumelles(inconnues)

    assert len(paires) == 1
    (majoritaire, ses_champs), (minoritaire, ses_autres) = paires[0]["formes"]
    assert (majoritaire, len(ses_champs)) == ("proprietaire", 2)
    assert (minoritaire, ses_autres) == ("propietaire", ["effectif"])


def test_le_releve_ne_DIT_PAS_laquelle_est_juste():
    """⚠️ L'ordre est un COMPTE, pas un verdict. oto ne connaît ni l'une ni l'autre :
    la forme minoritaire peut être la bonne, et une seule colonne peut avoir raison
    contre trois. Le message rend le fait ; la conclusion appartient à qui connaît le
    consommateur."""
    phrase = dsv2.cles_jumelles_warning(dsv2.cles_libres_jumelles(
        dsv2.unknown_declaration_keys(_schema(
            ("raison_sociale", "proprietaire"), ("effectif", "propietaire")))))

    # Le geste que ce message ne doit JAMAIS déclencher tout seul : un retrait. Six
    # attributs ont failli être supprimés sur la foi d'un avertissement voisin, un
    # seul était mort (cf. `cles_inconnues`). Ici la vérification passe AVANT.
    assert "avant de corriger" in phrase
    assert "ne peut donc pas dire laquelle" in phrase
    assert "les deux peuvent être délibérées" in phrase


def test_le_message_dit_la_CONSEQUENCE_pas_seulement_le_fait():
    """Sans elle, deux clés proches se lisent comme un détail de style. Ce qui change
    la réaction du lecteur, c'est de savoir qu'un décompte servi devient faux."""
    phrase = dsv2.cles_jumelles_warning(dsv2.cles_libres_jumelles(
        dsv2.unknown_declaration_keys(_schema(
            ("raison_sociale", "proprietaire"), ("effectif", "propietaire")))))

    assert "ABSENTE" in phrase and "EN ERREUR" in phrase
    assert "garde l'air juste" in phrase


def test_un_schema_SAIN_ne_produit_aucun_message():
    """Le cas de loin le plus fréquent : des clés libres franchement différentes."""
    inconnues = dsv2.unknown_declaration_keys(_schema(
        ("ca", "depends_on"), ("ca", "editable"), ("effectif", "initial_of")))

    assert dsv2.cles_libres_jumelles(inconnues) == []
    assert dsv2.cles_jumelles_warning([]) is None


# ── l'AXE, pas la forme : les chemins que mon échantillon ne traversait pas ───

def test_la_paire_est_vue_a_travers_un_SOUS_CHAMP():
    """⚠️ Mon premier échantillon ne posait les deux clés que sur des colonnes de
    premier niveau — l'axe exact sur lequel un défaut de production m'avait déjà
    échappé (`contacts[0].commentaire`). Le relevé descend dans `fields` et dans
    `of.fields` ; ce banc l'exige, sinon la garde n'existerait que là où j'ai regardé."""
    schema = {"fields": [
        {"key": "raison_sociale", "proprietaire": "x"},
        {"key": "contacts", "type": "list", "of": {"fields": [
            {"key": "email", "propietaire": "x"}]}},
    ]}
    paires = dsv2.cles_libres_jumelles(dsv2.unknown_declaration_keys(schema))

    assert len(paires) == 1
    porteuses = {c for _, champs in paires[0]["formes"] for c in champs}
    assert "contacts[].email" in porteuses


def test_les_DEUX_faces_servent_la_phrase():
    """Une garde qui ne parle qu'à l'auteur laisse le consommateur découvrir le défaut
    devant sa cliente — et c'est un consommateur qui a trouvé celui-ci. La pose et la
    lecture dérivent le même relevé."""
    import inspect
    from oto_mcp.capabilities.datastore import schema as face_lecture
    from oto_mcp.datastore import schema_ops

    for module in (face_lecture, schema_ops):
        assert "cles_jumelles_warning" in inspect.getsource(module), (
            f"{module.__name__} ne sert pas l'avertissement")

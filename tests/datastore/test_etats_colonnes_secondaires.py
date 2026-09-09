"""Une colonne qui déclare ses états les fait respecter, même sans être la file.

Le cas fondateur est celui de la campagne du 08/09/2026 : quatre tableaux de production
portent DEUX avancements pour DEUX acteurs — `statut`, la file que drainent les agents,
et `suivi`, les états commerciaux qu'un humain suit à l'écran. Les deux sont légitimes
depuis ce jour-là. Mais seule la file voyait sa valeur vérifiée : `suivi` était stockée,
servie, lue par son consommateur, et rien ne regardait son contenu.

⚠️ **Ce banc décrit une garde qui ne refuse RIEN d'existant** — mesuré avant de
l'écrire : 131 colonnes secondaires dans le parc, toutes avec une liste d'états, et
**zéro** valeur hors liste sur 8 646 cellules pleines. Ça ne la rend pas inutile, ça la
rend gratuite : elle ferme la porte avant qu'on la pousse.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2

# `statut` est la FILE (elle porte `claimable`) ; `suivi` est l'autre avancement.
SCHEMA = {"fields": [
    {"key": "statut", "lifecycle": {"states": ["a_faire", "fini"],
                                    "claimable": {"statut": "a_faire"}}},
    {"key": "suivi", "lifecycle": {"states": ["nouveau", "relance", "signe"],
                                   "terminal": ["signe"]}},
]}


def test_un_etat_de_la_liste_passe():
    assert dsv2.validate_row(SCHEMA, {"suivi": "relance"}) == []


def test_un_etat_hors_liste_est_refuse_et_le_refus_dit_ce_qu_il_accepte():
    errs = dsv2.validate_row(SCHEMA, {"suivi": "inconnu"})
    assert errs, "une colonne qui déclare ses états doit les faire respecter"
    (e,) = errs
    assert "suivi" in e and "inconnu" in e
    # le refus NOMME les états permis : sans eux, l'agent doit deviner ou relire le schéma
    assert "nouveau" in e and "relance" in e and "signe" in e


def test_la_valeur_se_deballe_comme_partout_ailleurs():
    """⚠️ Une cellule vaut `{"valeur": …, "comment": …}` en base. Juger la structure au
    lieu de son contenu est le défaut qui a arrêté une campagne entière le 29/08 : le
    contrôle refusait « état inconnu » sur la ligne que la plateforme venait elle-même
    de compléter. Zéro fiche écrite sur cent."""
    assert dsv2.validate_row(
        SCHEMA, {"suivi": {"valeur": "signe", "comment": "contrat reçu"}}) == []
    assert dsv2.validate_row(
        SCHEMA, {"suivi": {"valeur": "zzz", "comment": "x"}}) != []


def test_la_colonne_de_FILE_n_est_pas_jugee_DEUX_fois():
    """Elle est déjà traitée par `validate_row`, qui lui applique EN PLUS le contrôle
    de transition. Deux contrôles sur la même valeur rendraient deux fois le même
    refus — et un agent qui lit deux fois la même phrase croit à deux fautes."""
    errs = dsv2.validate_row(SCHEMA, {"statut": "zzz"})
    assert len(errs) == 1, errs


def test_les_deux_colonnes_se_jugent_dans_le_meme_geste():
    errs = dsv2.validate_row(SCHEMA, {"statut": "zzz", "suivi": "yyy"})
    assert len(errs) == 2 and any("statut" in e for e in errs) \
        and any("suivi" in e for e in errs)


def test_un_etat_devenu_invalide_GELE_au_lieu_de_bloquer_la_ligne():
    """Même partage que la file (07/09/2026) : « cet état est-il permis » juge ce que
    l'APPEL écrit. Un état stocké devenu invalide — parce qu'on l'a retiré de la liste
    depuis — gèlerait sinon la ligne entière, y compris pour une écriture sans rapport,
    et pour toujours."""
    gelees: list = []
    errs = dsv2.validate_row(SCHEMA, {"suivi": "ancien_etat", "statut": "fini"},
                             written={"statut"}, gelees=gelees)
    assert errs == [], "une valeur non écrite par ce geste ne doit pas refuser l'appel"
    assert gelees and gelees[0]["champ"] == "suivi"


def test_une_colonne_a_bloc_SANS_liste_d_etats_ne_contraint_rien():
    """`states` absent = rien de déclaré, donc rien à faire respecter. La garde s'arme
    sur une DÉCLARATION, jamais sur la simple présence d'un bloc."""
    schema = {"fields": [{"key": "suivi", "lifecycle": {"terminal": ["signe"]}}]}
    assert dsv2.validate_row(schema, {"suivi": "n_importe_quoi"}) == []


# ── les TRANSITIONS, sur les colonnes secondaires aussi ──────────────────────

TRANS = {"fields": [
    {"key": "statut", "lifecycle": {"states": ["a", "b"],
                                    "claimable": {"statut": "a"}}},
    {"key": "suivi", "lifecycle": {
        "states": ["nouveau", "relance", "signe"],
        "transitions": {"nouveau": ["relance"], "relance": ["signe"]}}},
]}


def test_une_transition_declaree_permise_passe():
    assert dsv2.validate_row(TRANS, {"suivi": "relance"},
                             avant={"suivi": "nouveau"}) == []


def test_un_saut_interdit_est_refuse_et_le_refus_dit_ce_qui_etait_permis():
    """⚠️ Mesuré avant d'écrire : **100 des 132 colonnes secondaires du parc déclarent
    des `transitions`** que rien ne vérifiait. Et la ligne d'avant était DÉJÀ chargée
    par le chemin d'écriture — on n'en extrayait qu'une colonne. Fermer ce cran n'a
    coûté aucune requête."""
    errs = dsv2.validate_row(TRANS, {"suivi": "signe"}, avant={"suivi": "nouveau"})
    assert errs and "transition" in errs[0]
    assert "nouveau" in errs[0] and "signe" in errs[0]
    assert "relance" in errs[0], "le refus doit nommer ce qui ÉTAIT permis"


def test_l_etat_d_avant_se_DEBALLE_lui_aussi():
    """Dès la deuxième écriture la ligne porte des couches : le cas normal est un objet,
    pas un mot. Lire la structure au lieu du contenu est le défaut qui a arrêté une
    campagne le 29/08 — deux gestes voisins qui lisent la même colonne doivent la lire
    pareil."""
    assert dsv2.validate_row(
        TRANS, {"suivi": "signe"},
        avant={"suivi": {"valeur": "nouveau", "comment": "reçu"}}) != []
    assert dsv2.validate_row(
        TRANS, {"suivi": "relance"},
        avant={"suivi": {"valeur": "nouveau", "comment": "reçu"}}) == []


def test_SANS_l_etat_d_avant_la_transition_ne_s_arme_PAS():
    """⚠️ Et c'est voulu : un appelant qui ne peut pas fournir l'état d'avant ne doit
    pas se voir refuser une transition qu'on est incapable de juger. L'appartenance,
    elle, reste vérifiée dans tous les cas."""
    assert dsv2.validate_row(TRANS, {"suivi": "signe"}) == []
    assert dsv2.validate_row(TRANS, {"suivi": "inconnu"}) != []


def test_rester_dans_le_meme_etat_n_est_pas_une_transition():
    assert dsv2.validate_row(TRANS, {"suivi": "nouveau"},
                             avant={"suivi": "nouveau"}) == []

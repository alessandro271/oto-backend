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


# ── les TRANSITIONS ne sont PAS jugées ici, et c'est mesuré ─────────────────

TRANS = {"fields": [
    {"key": "statut", "lifecycle": {"states": ["a", "b"],
                                    "claimable": {"statut": "a"}}},
    {"key": "suivi", "lifecycle": {
        "states": ["nouveau", "relance", "signe"],
        "terminal": ["signe"],
        "transitions": {"nouveau": ["relance"], "relance": ["signe"]}}},
]}


def test_revenir_d_un_etat_TERMINAL_reste_permis():
    """⚠️ Garde branchée le 09/09/2026 puis RETIRÉE le même jour, sur la mesure qui
    manquait. J'avais compté les DÉCLARATIONS — 100 colonnes sur 132 déclarent des
    `transitions` inertes — sans compter les LIGNES que le refus atteindrait :

        2 977 lignes · 62 tableaux · 15 propriétaires

    Et leurs états disent pourquoi c'était faux : `rejected`, `dropped`, `rejete`,
    `dead`, `skipped`. Refuser `signe → nouveau` n'interdit pas une transition métier :
    ça interdit de **réparer une erreur de saisie**.

    `transitions` déclare le parcours NORMAL, pas la liste des gestes permis. Lire
    « absent de `transitions` » comme « interdit » est une sur-interprétation — et sur
    un état terminal, dont la définition est de n'avoir aucune sortie, elle rend le
    retour arrière impossible par construction.

    ⚠️ **Ce banc ne passe plus d'état d'avant, et c'est le second temps du retrait** :
    le paramètre `avant` traversait quatre niveaux et n'était plus lu par personne.
    Un paramètre qui promet un jugement qu'il ne rend pas est de la même famille que
    l'import mort et le docstring périmé — du texte qui parle d'un mécanisme retiré.
    *Quand on retire une garde, ce qui reste doit être vérifié.*"""
    assert dsv2.validate_row(TRANS, {"suivi": "nouveau"}) == []
    assert dsv2.validate_row(TRANS, {"suivi": "signe"}) == []


def test_le_module_n_accepte_plus_d_etat_d_avant():
    """La signature dit ce que la fonction fait. Garder `avant` « au cas où » ferait
    croire à un jugement de transition qui n'existe plus."""
    import inspect
    from oto_mcp.datastore.etats_declares import etats_trahis
    assert "avant" not in inspect.signature(etats_trahis).parameters
    assert "avant" not in inspect.signature(dsv2.validate_row).parameters


def test_mais_l_APPARTENANCE_reste_verifiee_dans_tous_les_cas():
    """Ce qui a été retiré est le jugement du CHEMIN, pas celui de la valeur. Un état
    hors de la liste déclarée reste refusé — c'est la garde gratuite, mesurée à zéro
    violation sur 8 646 cellules."""
    assert dsv2.validate_row(TRANS, {"suivi": "inconnu"}) != []

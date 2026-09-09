"""Une faute de frappe en TÊTE de schéma désarmait tout le tableau, en silence (#97).

Le contrôle des clés inconnues ne parcourait que les COLONNES. Un réglage de tête mal
orthographié — `stricte` pour `strict` — était donc accepté sans un mot, et
`validation_active` rendait `False` : **toutes** les gardes du tableau tombaient d'un
coup — options, types, bornes, refus des colonnes inconnues. Ce n'est pas une protection
qui s'affaiblit, ce sont toutes ; et le propriétaire, lui, relit son schéma et y voit
`stricte: true`.

⚠️ **Mesuré sur le parc avant d'écrire la liste des clés permises**, parce qu'une liste
devinée ferait crier l'avertissement sur le régime normal — celui qu'on apprend à
ignorer. Sur 362 tableaux à schéma, deux clés de tête seulement sortaient de ce que le
code lit : `description` (15 tableaux, servie au front, donc DÉCLARÉE) et
`semantic_search` (1 tableau, qui est un paramètre d'appel rangé au mauvais endroit,
donc SIGNALÉ à juste titre). Un seul tableau du parc reçoit donc l'avertissement.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.cles_inconnues import check, inconnues_de_tete


def test_le_cas_fondateur_la_faute_de_frappe_sur_strict():
    """⚠️ Et le message doit dire la CONSÉQUENCE, pas seulement le nom : « clé inconnue »
    laisse croire à un détail, alors que le tableau entier est sans gardes."""
    msg = check({"stricte": True, "fields": [{"key": "a"}]})["unknown_keys_warning"]
    assert msg and "stricte" in msg
    assert "voulais-tu `strict`" in msg, "le nom proche est ce qui rend le refus actionnable"
    assert "ce réglage n'existe pas" in msg, "la conséquence, pas seulement le constat"


def test_la_consequence_annoncee_est_VRAIE():
    """⚠️ Le banc qui empêche le message de mentir à son tour : on vérifie que la garde
    est bien désarmée, au lieu de l'affirmer dans une phrase."""
    assert dsv2.validation_active({"stricte": True, "fields": [{"key": "a"}]}) is False
    assert dsv2.validation_active({"strict": True, "fields": [{"key": "a"}]}) is True


def test_un_PARAMETRE_d_appel_range_dans_le_schema_a_sa_propre_phrase():
    """`semantic_search` est un paramètre de `data_set_schema`. Posé dans le schéma il
    est stocké, servi, et sans effet — l'auteur croit avoir activé la recherche
    sémantique. « Clé inconnue » le ferait chercher une faute de frappe ; il faut lui
    dire que la clé est bonne et l'endroit mauvais. Un tableau du parc est dans ce cas."""
    d = inconnues_de_tete({"semantic_search": True})
    assert "PARAMÈTRE de `data_set_schema`" in d["semantic_search"]
    assert "sans aucun effet" in d["semantic_search"]


def test_une_cle_de_COLONNE_posee_en_tete_est_renvoyee_a_son_domicile():
    """Elle existe et elle est lue — ailleurs. Lui dire « inconnue » serait faux."""
    d = inconnues_de_tete({"readonly": True})
    assert "sur une COLONNE" in d["readonly"] and "fields" in d["readonly"]


def test_une_tete_PROPRE_ne_dit_rien():
    """⚠️ La moitié qui garantit qu'on ne crie pas sur le régime normal. `description`
    n'est lue par AUCUN contrôle du serveur et reste permise : 15 tableaux la portent,
    le schéma est servi tel quel, un écran peut l'afficher. « oto ne l'interprète pas »
    n'est pas « personne ne la lit » — la leçon des six attributs portés comme morts
    dont un seul l'était."""
    propre = {"key": "siren", "strict": True, "key_required": True,
              "unknown_fields": "reject", "description": "les éditeurs",
              "fields": [{"key": "siren", "type": "text"}]}
    assert inconnues_de_tete(propre) == {}
    assert check(propre)["unknown_keys_warning"] is None


def test_la_TETE_prime_sur_les_colonnes_dans_le_message():
    """⚠️ Leur conséquence n'est pas du même ordre : un attribut de colonne manqué
    désarme SA colonne, un réglage de tête manqué désarme le tableau ENTIER. Les noyer
    dans une même phrase ferait lire le plus grave comme un détail de plus."""
    msg = check({"stricte": True,
                 "fields": [{"key": "a", "zorglub": 1}]})["unknown_keys_warning"]
    assert msg.startswith("réglage(s) de TÊTE"), msg[:60]
    assert "par ailleurs" in msg, "les colonnes restent mentionnées, en second"


def test_la_liste_permise_est_DERIVEE_de_la_declaration():
    """Jamais une liste recopiée à côté : le jour où un réglage de tête s'ajoute, il
    entre par la déclaration et l'avertissement s'éteint tout seul."""
    from oto_mcp.datastore import schema_keys as sk
    assert sk.TETE_RECONNUES == frozenset(c.nom for c in sk.CLES_DE_TETE)
    for lue in ("strict", "key", "fields", "unknown_fields", "key_required"):
        assert lue in sk.TETE_RECONNUES, f"`{lue}` est LUE par le code et doit être permise"

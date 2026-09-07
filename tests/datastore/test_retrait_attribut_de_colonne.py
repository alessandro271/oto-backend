"""Retirer un ATTRIBUT d'une colonne, sans reposer le schéma entier.

Le trou que ça ferme, et il était béant. Deux gestes existaient pour modifier un
schéma, et aucun ne savait enlever un mot :

- **fusionner** COMPLÈTE — « les propriétés fournies écrasent, les autres sont
  préservées ». Reposter une colonne sans son `role` ne retire donc pas le `role` ;
- **retirer** enlève une COLONNE ENTIÈRE, pas une de ses propriétés.

Restait le remplacement complet du schéma — c'est-à-dire le geste qui a détruit 78
notes de champ dans un incident, puis 52 dans un autre, en silence (#388). *On avait
troqué la destruction accidentelle contre l'impossibilité de retirer un mot.*

Découvert en cherchant à exécuter une demande du propriétaire du produit — sortir du
schéma des rôles que plus rien ne lit. La demande était simple ; la plateforme ne
savait pas la servir autrement qu'en risquant tout le reste.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2

CHAMPS = [
    {"key": "priorite", "type": "enum", "options": ["A", "B"], "role": "qualif",
     "label": "Priorité", "description": "une note longue qu'on ne veut pas perdre"},
    {"key": "statut", "type": "enum", "role": "status",
     "lifecycle": {"states": ["a", "b"], "terminal": ["b"]}},
]


def test_retire_l_attribut_nomme_et_RIEN_d_autre():
    """Le cœur : ce qui n'est pas nommé survit. C'est toute la différence avec le
    remplacement du schéma, qui perd tout ce qu'on n'a pas repensé à renvoyer."""
    out, inconnus = dsv2.remove_field_attrs(CHAMPS, {"priorite": ["role"]})

    assert inconnus == []
    priorite = next(f for f in out if f["key"] == "priorite")
    assert "role" not in priorite
    # la note longue, le libellé, les options, le type : intacts
    assert priorite["label"] == "Priorité"
    assert priorite["description"] == "une note longue qu'on ne veut pas perdre"
    assert priorite["options"] == ["A", "B"]
    assert priorite["type"] == "enum"


def test_les_autres_colonnes_ne_bougent_pas():
    out, _ = dsv2.remove_field_attrs(CHAMPS, {"priorite": ["role"]})

    statut = next(f for f in out if f["key"] == "statut")
    assert statut["role"] == "status"          # l'ancre de la file, intacte
    assert statut["lifecycle"]["states"] == ["a", "b"]


@pytest.mark.parametrize("demande,attendu", [
    ({"priorite": ["rôle"]}, ["priorite.rôle"]),      # faute sur l'attribut
    ({"prioritee": ["role"]}, ["prioritee"]),          # faute sur la colonne
])
def test_une_faute_de_frappe_ne_passe_pas_en_silence(demande, attendu):
    """La moitié du travail d'une garde est de refuser de mentir sur ce qu'elle a
    fait. Un retrait silencieux sur un nom mal tapé ferait croire au nettoyage — et
    on découvrirait le mot toujours là des semaines plus tard, en cherchant pourquoi
    quelque chose s'appuie encore dessus."""
    _, inconnus = dsv2.remove_field_attrs(CHAMPS, demande)

    assert inconnus == attendu


def test_la_cle_ne_se_retire_pas():
    """`key` n'est pas une propriété de la colonne, c'est son identité. La retirer
    rendrait le field inadressable : il resterait au schéma sans que personne puisse
    le désigner pour le réparer."""
    with pytest.raises(ValueError) as e:
        dsv2.remove_field_attrs(CHAMPS, {"priorite": ["key"]})

    assert "identité de la colonne" in str(e.value)


def test_le_releve_d_effacement_se_TAIT_sur_un_retrait_annonce():
    """Un avertissement qui crie sur un geste explicite est celui qu'on apprend à
    ignorer — donc celui qui ruine les vrais. Le relevé ne se tait QUE sur ce que
    l'appelant a nommé."""
    out, _ = dsv2.remove_field_attrs(CHAMPS, {"priorite": ["role"]})
    ancien, nouveau = {"fields": CHAMPS}, {"fields": out}

    sans = dsv2.declarations_effacees(ancien, nouveau)
    assert sans and sans[0]["declarations"] == {"role": "qualif"}

    avec = dsv2.declarations_effacees(ancien, nouveau, ["priorite.role"])
    assert avec == []


def test_le_releve_reste_TENDU_sur_ce_qui_n_est_pas_annonce():
    """Le filet ne s'affaiblit pas : annoncer un retrait n'aveugle que celui-là.
    Sans ce cas, taire l'annoncé se lirait comme taire tout ce qui disparaît."""
    out, _ = dsv2.remove_field_attrs(CHAMPS, {"priorite": ["role", "label"]})
    ancien, nouveau = {"fields": CHAMPS}, {"fields": out}

    # on n'annonce QUE le rôle : la perte du libellé doit rester criée
    reste = dsv2.declarations_effacees(ancien, nouveau, ["priorite.role"])

    assert reste and reste[0]["declarations"] == {"label": "Priorité"}

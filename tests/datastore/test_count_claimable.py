"""Compter ce que `claim_next` servirait — avec SA clause, pas une copie.

**D'où vient cette fonction.** Une campagne dont la file est vide continuait de
fabriquer des travaux jusqu'à son plafond : la production ne regardait pas s'il restait
des lignes. Mesuré le 09/09/2026 par l'ordonnanceur — 52 campagnes armées produisant en
continu des déroulés qui concluaient « file vide » en quelques secondes, jusqu'à ~50
refus toutes les quatre minutes sur la production.

**Pourquoi elle vit ICI et pas chez l'appelant**, et c'est lui qui l'a formulé : le
périmètre réclamable compose quatre choses — le filtre de la campagne, le `claimable` du
schéma, le filet `abandon_reason IS NULL`, et les baux actifs. *« Si je recopie cette
clause, elle divergera — c'est certain, pas probable — et une divergence ferait arrêter
une campagne qui a encore du travail. Une garde qui coupe à tort est pire que
l'absence de garde. »*

⚠️ **Ce banc garde surtout l'INSÉPARABILITÉ des deux clauses.** Le jour où quelqu'un
ajoutera un cran à `claim_next` sans le répercuter, c'est ici que ça doit rougir — pas
en production, sur une campagne arrêtée à tort.
"""
from __future__ import annotations

import inspect

from oto_mcp.db import rowlock


def test_les_deux_gestes_appellent_LA_MEME_definition_de_perimetre():
    """⚠️ Le cœur de la demande. Si l'un des deux cesse de passer par
    `_perimetre_reclamable`, les clauses peuvent diverger en silence — et une
    divergence arrête une campagne qui a du travail."""
    for fn in (rowlock.datastore_claim_next, rowlock.datastore_count_claimable):
        src = inspect.getsource(fn)
        assert "_perimetre_reclamable(" in src, (
            f"`{fn.__name__}` ne compose plus le périmètre par la définition partagée : "
            f"c'est exactement la divergence que cette fonction existe pour empêcher.")


def test_le_perimetre_porte_les_quatre_composantes():
    where, params = rowlock._perimetre_reclamable(
        7, [{"field": "statut", "op": "eq", "value": "a_faire"}])
    # le tableau visé
    assert "ns_id = %s" in where and params[0] == 7
    # le filet de PLATEFORME, indépendant du filtre du client
    assert "abandon_reason IS NULL" in where
    # les baux actifs
    assert "claimed_until IS NULL OR claimed_until < NOW()" in where
    # le filtre de l'appelant, whitelisté par le même chemin que la lecture
    assert "a_faire" in params


def test_le_comptage_exclut_ce_que_la_passe_d_abandon_retirerait():
    """⚠️ La subtilité qui rend le chiffre juste. `claim_next` ABANDONNE les lignes à
    bout avant de piocher : elles sortent alors du périmètre par `abandon_reason`. Le
    comptage, lui, n'écrit rien — un comptage qui écrit serait un piège. Il doit donc
    reproduire l'effet de cette passe sans la jouer, sinon il SUR-ESTIME.

    Et sur-estimer est précisément le défaut qu'on ferme : la campagne continuerait de
    produire des travaux pour des lignes que `claim_next` refusera."""
    sans, _ = rowlock._perimetre_reclamable(7, None)
    avec, params = rowlock._perimetre_reclamable(7, None, hors_plafond=3)
    assert "claims <" not in sans, "le pick n'exclut pas le plafond — la passe l'a déjà fait"
    assert "claims < %s" in avec and params[-1] == 3


def test_le_comptage_n_ecrit_rien():
    """Il ne doit ni réserver, ni abandonner, ni poser de bail : un comptage qui a des
    effets de bord est un piège pour qui le lit dans une condition."""
    src = inspect.getsource(rowlock.datastore_count_claimable)
    for interdit in ("UPDATE", "INSERT", "DELETE", "abandonner_les_lignes_a_bout"):
        assert interdit not in src, f"`{interdit}` n'a rien à faire dans un comptage"


def test_la_signature_repond_a_la_question_posee_et_pas_plus():
    """« Reste-t-il quelque chose à réserver » — un entier. Pas de pagination, pas de
    lignes : ce que l'appelant a demandé, exactement."""
    sig = inspect.signature(rowlock.datastore_count_claimable)
    assert list(sig.parameters) == ["ns_id", "filters", "max_claims"]
    assert sig.return_annotation in ("int", int)


# ── le refus doit accuser le SCHÉMA, jamais l'appelant ───────────────────────

def test_un_perimetre_illisible_accuse_le_SCHEMA_et_dit_de_ne_pas_reessayer():
    """⚠️ Incident du 09/09/2026, et son coût est dans ce qu'il produit chez qui le
    reçoit. Un tableau portait `claimable: ["a_traiter"]` — une liste là où la grammaire
    attend `{col: val}`. La fonction levait un `AttributeError` nu, et **dix agents ont
    conclu que leur APPEL était fautif** : 60 refus sur 81 appels, huit par travail,
    jusqu'au plafond de tours. Les dix se sont conclus « terminé » sans une écriture.

    Un refus qui décrit la FORME ATTENDUE d'un paramètre fait chercher la faute chez
    celui qui appelle. Quand elle est dans une déclaration qu'il ne contrôle pas, le
    refus doit dire OÙ elle est et QUI peut la corriger."""
    import pytest
    from oto_mcp.datastore import claimable

    with pytest.raises(ValueError) as exc:
        claimable.clauses(["a_traiter"])
    msg = str(exc.value)
    assert "SCHÉMA" in msg, "le refus doit nommer le coupable, pas la forme attendue"
    assert "Ton appel n'y est pour rien" in msg, "sans ça, l'agent varie sa formulation"
    assert "data_patch_schema" in msg, "le refus doit dire par quel geste on corrige"


def test_un_perimetre_bien_forme_passe_toujours():
    from oto_mcp.datastore import claimable
    assert claimable.clauses({"statut": "a_traiter"})
    assert claimable.clauses(None) == []

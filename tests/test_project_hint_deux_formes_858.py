"""Un lien de tableau se désigne par son NOM ou par son ID — le hint comparait l'un
et recevait l'autre (#858).

Mesuré le 10/09/2026 : le tableau 654 était lié au projet 448 depuis sept minutes
(lien visible dans `oto_project op=get`), et chaque `data_write(datastore=654,
_project=448, …)` répondait « ce tableau `654` n'est pas lié au projet actif
(#448) ». Cause : `list_project_links` enrichit chaque lien `tableau` du NOM de son
tableau, et l'écriture le visait par son id.

⚠️ **Le coût d'un hint qui se trompe n'est pas le bruit, c'est l'action qu'il
provoque.** Quatre sous-agents successifs l'ont cru, deux l'ont remonté comme une
tâche à faire, un a proposé de créer le lien — un doublon évité de justesse. Un
conseil faux dans une réponse réussie se suit, parce que rien ne le contredit.

⚠️ **Et la connaissance manquante était trente lignes plus haut** :
`_anon_project_tableau_ns_ids` résout DÉJÀ les deux formes et le dit dans sa
docstring (« soit l'id numérique du datastore, soit son NOM »). Le défaut n'est pas
une ignorance, c'est une duplication de lecture qui a divergé.

⚠️ **Reste un cas indécidable, et là le hint se TAIT** : cible numérique contre
lien nommé, qu'on ne peut pas rapprocher sans une requête de plus. Affirmer y
rejouerait le défaut ; se taire ne coûte qu'un rappel, et ce hint n'a jamais été
bloquant.

Éprouvé rouge le 2026-09-10 : la comparaison réduite au seul nom ⟹ le premier test
nomme le tableau déclaré non lié alors qu'il l'est.
"""
from __future__ import annotations

import pytest

from oto_mcp.tools import datastore as ds


@pytest.fixture()
def projet_actif(monkeypatch):
    monkeypatch.setattr(ds.access, "current_project", lambda: 448)
    return 448


def _liens(monkeypatch, liens):
    monkeypatch.setattr(ds.db, "list_project_links", lambda pid: liens)


def test_lie_par_ID_et_ecrit_par_ID_ne_declenche_rien(projet_actif, monkeypatch):
    """Le cas mesuré, exactement : le lien porte l'id en `target_ref`, le nom est
    posé à côté, et l'écriture vise l'id."""
    _liens(monkeypatch, [{"target_type": "tableau", "target_ref": "654",
                          "datastore": "vivier-industrie-pdl-paca"}])
    assert ds._project_hint("654") is None
    assert ds._project_hint(654) is None, "un id numérique non stringifié passe aussi"


def test_lie_par_ID_et_ecrit_par_NOM_ne_declenche_rien(projet_actif, monkeypatch):
    """Le symétrique : c'est le nom enrichi qui répond."""
    _liens(monkeypatch, [{"target_type": "tableau", "target_ref": "654",
                          "datastore": "vivier-industrie-pdl-paca"}])
    assert ds._project_hint("vivier-industrie-pdl-paca") is None


def test_un_tableau_VRAIMENT_non_lie_est_toujours_signale(projet_actif, monkeypatch):
    """La contre-épreuve : le lot ne doit pas éteindre la suggestion, seulement
    cesser de la donner à tort."""
    _liens(monkeypatch, [{"target_type": "tableau", "target_ref": "654",
                          "datastore": "vivier-industrie-pdl-paca"}])
    hint = ds._project_hint("autre-tableau")
    assert hint and "op=link" in hint and "#448" in hint


def test_le_cas_INDECIDABLE_se_tait(projet_actif, monkeypatch):
    """Cible numérique, liens tous nommés : on ne peut pas rapprocher sans résoudre.
    Le silence est le bon défaut — affirmer rejouerait le défaut corrigé."""
    _liens(monkeypatch, [{"target_type": "tableau", "target_ref": "leads",
                          "datastore": "leads"}])
    assert ds._project_hint("654") is None


def test_un_lien_qui_n_est_PAS_un_tableau_ne_compte_pas(projet_actif, monkeypatch):
    """Une procédure dont la ref vaut `654` ne rend pas le tableau 654 lié — sinon
    un identifiant partagé entre deux types ferait taire le hint au hasard."""
    _liens(monkeypatch, [{"target_type": "procedure", "target_ref": "654"}])
    hint = ds._project_hint("654")
    assert hint and "op=link" in hint


def test_hors_projet_le_hint_reste_muet(monkeypatch):
    monkeypatch.setattr(ds.access, "current_project", lambda: None)
    assert ds._project_hint("654") is None

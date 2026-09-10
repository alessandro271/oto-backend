"""La recherche de procédures ne propose plus une procédure ARCHIVÉE (#857).

L'archivage promet que la procédure « cesse d'être proposée ». Trois lectures
tenaient cette promesse — la liste et la recherche du store, et la recherche des
projets, voisine immédiate de celle-ci. **La recherche plein texte des procédures ne
la tenait pas.**

⚠️ **Une promesse vraie sur trois surfaces et fausse sur une quatrième est le pire
état possible** : elle ne se découvre ni par le code ni par un test, seulement par
hasard, le jour où quelqu'un retravaille une procédure retirée en croyant qu'elle
sert. C'est arrivé : 3 lignes archivées sur 238 en production, dont **deux réécrites
après coup** par quelqu'un qui les croyait en service.

⚠️ **Ce banc garde la clause là où elle manquait, pas la promesse en général.** Le
contrôle qui compte est la comparaison avec la voisine des projets : les deux
requêtes sont écrites à quatre lignes d'écart, et c'est l'écart entre elles qui a
produit le défaut.

Éprouvé rouge le 2026-09-10 : la clause retirée ⟹ le premier test nomme la requête
qui propose une procédure retirée.
"""
from __future__ import annotations

import inspect

from oto_mcp.db import search as S


def _source(fn) -> str:
    return " ".join(inspect.getsource(fn).split())


def test_la_recherche_de_procedures_EXCLUT_les_archivees():
    assert "AND archived_at IS NULL" in _source(S.search_procedures_fts), (
        "cette requête propose une procédure retirée du service")


def test_elle_le_fait_COMME_sa_voisine_des_projets():
    """L'écart entre deux requêtes voisines est ce qui a produit le défaut : on garde
    donc la parité, pas seulement la présence de la clause."""
    assert "archived_at IS NULL" in _source(S.search_project_briefs)
    assert "archived_at IS NULL" in _source(S.search_procedures_fts)


def test_la_clause_ne_remplace_pas_le_filtre_des_RELIQUES():
    """Le filtre qui écarte les reliques du readme pré-convergence doit survivre —
    un lot qui ajoute une condition ne doit pas en perdre une autre."""
    src = _source(S.search_procedures_fts)
    assert "slug <> 'claude_md'" in src


def test_le_scope_de_l_org_reste_intact():
    src = _source(S.search_procedures_fts)
    assert "owner_type = 'org'" in src and "owner_id = %s" in src

"""Archiver une page hors de la recherche, et dater sa vérification (signal #1087).

Une page périmée remontait dans `oto_search` à égalité avec une page fraîche, et la seule
sortie était `op=delete`, irréversible. Ce banc tient, sur base RÉELLE et par la capacité
servie (`core._doc`, droits réels — l'appelant possède le projet) :

- archiver retire la page ET son sous-arbre vivant de `oto_search` (lexical) et de la
  liste par défaut ; la page se lit encore par son id ; la liste dit combien elle cache ;
- désarchiver rend exactement ce geste-là, pas une sous-page archivée avant lui ;
- on ne range pas une page vivante sous une archivée (création, déplacement,
  désarchivage d'une enfant) ;
- vérifier pose `verified_at`/`verified_by` sans toucher au corps, à `updated_at` ni aux
  versions ;
- un tiers sans droit d'écriture ne peut ni archiver ni vérifier.

Éprouvé rouge le 2026-10-23 : la clause `archived_at IS NULL` retirée de
`search_docs_fts` ⟹ `test_archivee_sort_de_la_recherche_lisible_par_son_id` la retrouve.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import db
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.docs import core as D

MOI, TIERS = "u-archivage-moi", "u-archivage-tiers"
MOT = f"perime{uuid.uuid4().hex[:8]}"
CTX = ResolvedCtx(sub=MOI, org_id=None)


@pytest.fixture(scope="module")
def projet(live):
    for sub in (MOI, TIERS):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    return int(db.create_project("user", MOI, f"Mission {MOT}", created_by=MOI))


def _page(pid, titre, parent=None):
    return db.create_doc(pid, f"{titre} {MOT}", parent_id=parent,
                         body_md=f"Dernier état connu {MOT}", created_by=MOI)


def _doc(ctx=CTX, **kw):
    return D._doc(ctx, D.DocInput(**kw))


def _trouvees(pid):
    return {h["id"] for h in db.search_docs_fts(MOT, [pid], limit=50)}


def _listees(pid, **kw):
    return {d["id"] for d in _doc(op="list", project_id=pid, **kw)["docs"]}


def test_archivee_sort_de_la_recherche_lisible_par_son_id(projet):
    racine = _page(projet, "Portfolio")
    enfant = _page(projet, "Portfolio détail", racine)
    voisine = _page(projet, "Fraîche")
    assert {racine, enfant, voisine} <= _trouvees(projet)

    out = _doc(op="archive", doc_id=racine)
    assert (out["archived"], out["descendants"]) == (True, 1)

    assert not ({racine, enfant} & _trouvees(projet)), "archiver = hors de la recherche"
    assert voisine in _trouvees(projet)
    lue = _doc(op="get", doc_id=racine)
    assert lue["archived_at"] and lue["body_md"].startswith("Dernier état connu")

    liste = _doc(op="list", project_id=projet)
    assert not ({racine, enfant} & {d["id"] for d in liste["docs"]})
    assert liste["archived_hidden"] >= 2, "la liste dit ce qu'elle cache"
    assert {racine, enfant} <= _listees(projet, include_archived=True)


def test_archiver_est_idempotent_et_garde_la_premiere_date(projet):
    p = _page(projet, "Idempotente")
    premiere = _doc(op="archive", doc_id=p)["archived_at"]
    encore = _doc(op="archive", doc_id=p)
    assert (encore["archived"], encore["archived_at"]) == (False, premiere)


def test_desarchiver_rend_ce_geste_la_et_pas_un_archivage_anterieur(projet):
    racine = _page(projet, "Arbre")
    vieille = _page(projet, "Archivée avant", racine)
    vive = _page(projet, "Vivante", racine)
    _doc(op="archive", doc_id=vieille)
    _doc(op="archive", doc_id=racine)
    out = _doc(op="unarchive", doc_id=racine)
    assert (out["unarchived"], out["descendants"]) == (True, 1)
    assert {racine, vive} <= _trouvees(projet)
    assert vieille not in _trouvees(projet), "archivée pour son compte, elle le reste"


def test_desarchiver_une_page_vivante_n_est_pas_une_erreur(projet):
    p = _page(projet, "Jamais archivée")
    assert _doc(op="unarchive", doc_id=p)["unarchived"] is False


def test_pas_de_page_vivante_sous_une_archivee(projet):
    parent = _page(projet, "Parent archivé")
    enfant = _page(projet, "Enfant", parent)
    libre = _page(projet, "Libre")
    _doc(op="archive", doc_id=parent)
    for kw in ({"op": "create", "project_id": projet, "title": "x", "parent_id": parent},
               {"op": "bulk_create", "project_id": projet, "parent_id": parent,
                "pages": [{"title": "y"}]},
               {"op": "move", "doc_id": libre, "parent_id": parent},
               {"op": "unarchive", "doc_id": enfant}):
        with pytest.raises(AuthzDenied) as e:
            _doc(**kw)
        assert (e.value.status, e.value.code) == (409, "parent_archived"), kw


def test_verifier_date_sans_toucher_au_contenu(projet):
    p = _page(projet, "À revalider")
    avant = db.get_doc_by_id(p)
    out = _doc(op="verify", doc_id=p)
    assert out["verified_by"] == MOI and out["verified_at"]
    apres = db.get_doc_by_id(p)
    assert (apres["updated_at"], apres["body_md"]) == (avant["updated_at"],
                                                       avant["body_md"])
    assert _doc(op="revisions", doc_id=p)["revisions"] == []
    assert _doc(op="get", doc_id=p)["verified_at"] == out["verified_at"]


def test_un_tiers_ne_peut_ni_archiver_ni_verifier(projet):
    p = _page(projet, "Privée")
    tiers = ResolvedCtx(sub=TIERS, org_id=None)
    for op in ("archive", "unarchive", "verify"):
        with pytest.raises(AuthzDenied) as e:
            _doc(tiers, op=op, doc_id=p)
        assert e.value.status == 404, "illisible = inexistante (F-016)"


def test_include_archived_ne_vaut_que_pour_list(projet):
    p = _page(projet, "Option")
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", doc_id=p, include_archived=True)
    assert e.value.code == "unsupported_include_archived"

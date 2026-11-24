"""Recherche sémantique (lot 3 V2) — fusion RRF lexical+sémantique (dédup + somme
des rangs), dégradation gracieuse sans embedding. Le worker d'indexation :
`test_embed_worker.py`.
DB stubée ; l'API Mistral n'est jamais appelée."""

from oto_mcp import search as S, embeddings as E


def _stub_sources(monkeypatch, *, lexical=(), semantic=()):
    monkeypatch.setattr(S.ownership, "accessible_project_ids", lambda *a, **k: [1])
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: [dict(r) for r in lexical])
    monkeypatch.setattr(S.db, "search_docs_semantic",
                        lambda ql, pids, limit: [dict(r) for r in semantic])
    for n in ("search_project_briefs", "search_procedures_fts", "search_guides_fts",
              "search_files_meta", "search_file_contents",
              "search_briefs_semantic", "search_guides_semantic"):
        monkeypatch.setattr(S.db, n, lambda *a, **k: [])
    monkeypatch.setattr(S.ownership, "principaux_de_liste", lambda *a: [])
    monkeypatch.setattr(S.ownership, "perso_de_la_liste", lambda *a: [])
    # Mes tableaux perso créés dans l'org (org perso = org, 29/09/2026) : une source
    # de plus pour la parité liste/recherche, donc une doublure de plus — sans elle,
    # `mes_objets_ici` lirait l'étiquette perso en base.
    monkeypatch.setattr(S.ownership, "mes_objets_ici", lambda *a: None)
    monkeypatch.setattr(S.ownership, "mes_tableaux_ici", lambda *a: [])
    monkeypatch.setattr(S.db, "list_datastores_for_owners", lambda o: [])
    # Partagés à moi en personne (06/11/2026, `ownership.partages_a_moi_ici`).
    monkeypatch.setattr(S.db, "list_datastores_shared_to_user", lambda sub: [])
    monkeypatch.setattr(S.db, "list_datastores_granted_to", lambda *a: [])
    monkeypatch.setattr(S.db, "project_labels", lambda ids: {})
    # La sonde des sources brutes non parcourues (12/11/2026).
    monkeypatch.setattr(S.db, "sources_presentes", lambda pids: False)


def _page(id, title, headline=None):
    return {"id": id, "project_id": 1, "title": title, "headline": headline}


def test_page_found_by_both_fuses_and_ranks_first(monkeypatch):
    # Page 10 : rang 1 lexical ET rang 1 sémantique → somme → devant la page 11 (lexical seul).
    _stub_sources(monkeypatch,
                  lexical=[_page(10, "A", "<b>x</b>"), _page(11, "B", "<b>x</b>")],
                  semantic=[_page(10, "A")])
    out = S.search("u1", 7, "requête", query_embedding=[0.1] * 3)
    ids = [h["ref"] for h in out["hits"]]
    assert ids[0] == 10                       # cumulé lexical+sémantique
    assert ids.count(10) == 1                 # dédupliqué (pas 2 entrées)
    # le passage lexical est conservé sur la ligne fusionnée
    assert next(h for h in out["hits"] if h["ref"] == 10)["passage"] == "<b>x</b>"


def test_semantic_only_page_appears(monkeypatch):
    # Une page trouvée UNIQUEMENT par le sens (aucun mot exact) remonte quand même.
    _stub_sources(monkeypatch, lexical=[], semantic=[_page(20, "Dirigeant")])
    out = S.search("u1", 7, "décideur", query_embedding=[0.2] * 3)
    assert [h["ref"] for h in out["hits"]] == [20]
    assert out["hits"][0]["matched_by"] == "semantic"


def test_no_embedding_is_lexical_only(monkeypatch):
    called = {"sem": False}
    _stub_sources(monkeypatch, lexical=[_page(10, "A", "<b>x</b>")])
    monkeypatch.setattr(S.db, "search_docs_semantic",
                        lambda *a, **k: called.update(sem=True) or [])
    out = S.search("u1", 7, "x", query_embedding=None)     # pas de vecteur
    assert called["sem"] is False and [h["ref"] for h in out["hits"]] == [10]


def test_to_pg_literal():
    assert E.to_pg([1.0, 2.5, -3.0]).startswith("[1.0,2.5,")

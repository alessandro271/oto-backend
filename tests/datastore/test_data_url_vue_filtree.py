"""`data_url` rend une vue FILTRÉE que la page tableau sait lire (signal 1355).

La page tableau du tableau de bord relit sa vue dans l'URL à l'ouverture (oto-app,
`DatastoreTable.vue::readTableQuery`) : `f` (triplets `[champ, op, valeur]` en JSON,
`lib/datastoreFilters.ts::filtersFromParam`), `q`, `sort`/`dir`. Ces bancs relisent le
lien COMME la page le relit — y compris ce qu'elle ÉCARTE sans rien dire (une valeur
vide, un opérateur inconnu) —, appellent le tool MONTÉ sur un vrai store, et tiennent
les deux refus : ce que la page écarterait, et un produit qui ne lit pas ce format.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from oto_mcp.datastore import vue_par_url as V
from oto_mcp.mcp_errors import McpError

#: `lib/datastoreFilters.ts` : `ALL_OPS` = les opérateurs de `OPS_BY_KIND` plus `in`
#: (miroir de `db.query._DS_FILTER_OPS`).
_OPS_DE_LA_PAGE = {"contains", "eq", "ne", "in", "gt", "gte", "lt", "lte", "empty",
                   "not_empty"}


def _relu_par_la_page(url: str) -> dict:
    """`filtersFromParam` + `readTableQuery`, réécrits : ce que la page en garde."""
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    gardes = []
    for t in json.loads(q.get("f") or "[]"):
        champ, op = t[0], t[1]
        if not isinstance(champ, str) or not champ or op not in _OPS_DE_LA_PAGE:
            continue
        valeur = [str(v) for v in t[2]] if isinstance(t[2], list) else str(t[2] or "")
        if op not in ("empty", "not_empty") and not valeur:
            continue
        gardes.append([champ, op, valeur])
    return {"filtres": gardes, "q": q.get("q", ""), "sort": q.get("sort"),
            "dir": q.get("dir")}


def test_le_format_est_celui_que_la_page_relit():
    url = V.avec_la_vue(
        "https://dash.test/data/7",
        filter={"statut": {"in": ["a_faire", "en_cours"]}, "pole": "Ventes & co",
                "clos": False, "note": {"gte": 3}, "email": {"empty": True}},
        q="leads été", order_by="pole", order_dir="asc")
    assert url.startswith("https://dash.test/data/7?")
    assert "+" not in url, "une espace s'écrit %20 : la page lit `+` comme une espace"
    assert _relu_par_la_page(url) == {
        "filtres": [["statut", "in", ["a_faire", "en_cours"]],
                    ["pole", "eq", "Ventes & co"], ["clos", "eq", "false"],
                    ["note", "gte", "3"], ["email", "empty", ""]],
        "q": "leads été", "sort": "pole", "dir": "asc"}


@pytest.mark.parametrize("filtre", [{"statut": ""}, {"statut": None},
                                    {"statut": {"in": []}}, {"statut": {"eq": {"a": 1}}}],
                         ids=["vide", "null", "in-vide", "objet"])
def test_ce_que_la_page_ecarterait_est_refuse(filtre):
    with pytest.raises(ValueError):
        V.triplets(filtre)


def test_un_sens_de_tri_inconnu_est_refuse():
    with pytest.raises(ValueError):
        V.avec_la_vue("https://d/data/1", filter=None, q=None, order_by="x",
                      order_dir="up")


# ── Le tool MONTÉ, sur un vrai store ─────────────────────────────────────────────

@pytest.fixture()
def tool(live, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, links
    from oto_mcp.datastore.core import DatastorePg
    from oto_mcp.db._conn import _connect
    from oto_mcp.tools import datastore as surface

    with _connect() as conn:
        conn.execute("DELETE FROM datastore_rows WHERE ns_id = 9101")
        conn.execute("DELETE FROM user_datastores WHERE id = 9101")
        conn.execute("INSERT INTO user_datastores (id, owner_type, owner_id, namespace) "
                     "VALUES (9101, 'user', 'u-1', 'sujets')")
        for i, (statut, pole) in enumerate([("a_faire", "A"), ("en_cours", "B"),
                                            ("fait", "A"), ("abandonne", "B")]):
            conn.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES "
                         "(9101, %s, %s::jsonb)",
                         (f"r{i}", json.dumps({"statut": statut, "pole": pole})))
    store = DatastorePg("u-1")
    def _resolve(ns, write=False):
        # Le vrai `_resolve` relève aussi le tableau résolu (`core.py`) : on le refait.
        store.dernier_tableau = {"ns_id": 9101, "datastore": "sujets"}
        return 9101

    monkeypatch.setattr(store, "_resolve", _resolve)
    monkeypatch.setattr(store, "_ns_of", lambda ns_id: {"schema": None, "datastore": "sujets"})
    monkeypatch.setattr(store, "_org_des_liens", lambda: None)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u-1")
    monkeypatch.setattr(surface, "_store_for", lambda sub: store)
    monkeypatch.setattr(links, "_tenant_of", lambda sub: None)     # notre produit
    mcp = FastMCP("sonde")
    surface.register(mcp)
    return asyncio.run(mcp.get_tool("data_url")).fn


def test_sans_vue_la_reponse_ne_change_pas(tool):
    out = tool(datastore="sujets")
    assert set(out) == {"url"} and urlsplit(out["url"]).query == ""


def test_la_vue_non_close_et_le_compte_de_ce_qu_elle_montre(tool):
    out = tool(datastore="sujets", filter={"statut": {"in": ["a_faire", "en_cours"]}},
               order_by="pole", order_dir="asc")
    assert out["count"] == 2 and out["ns_id"] == 9101
    assert urlsplit(out["url"]).path.endswith("/data/9101")
    assert _relu_par_la_page(out["url"])["filtres"] == [
        ["statut", "in", ["a_faire", "en_cours"]]]


def test_un_operateur_inconnu_est_refuse_par_le_vrai_chemin(tool):
    with pytest.raises(McpError):
        tool(datastore="sujets", filter={"statut": {"pas_un_op": "x"}})


def test_une_colonne_inconnue_est_refusee(tool):
    with pytest.raises(McpError, match="statu"):
        tool(datastore="sujets", filter={"statu": "fait"})
    with pytest.raises(McpError, match="pol"):
        tool(datastore="sujets", order_by="pol")


def test_un_produit_qui_ne_lit_pas_la_vue_recoit_un_refus(tool, monkeypatch):
    from oto_mcp import links
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, '{"table": "/tables/{id}"}')
    assert links.table_lit_sa_vue("u-1") is False
    assert tool(datastore="sujets")["url"].endswith("/tables/9101"), "le lien nu reste"
    with pytest.raises(McpError):
        tool(datastore="sujets", filter={"statut": "fait"})

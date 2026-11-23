"""Les dernières routes publiques de FOD qu'aucun outil n'appelait (france-opendata-service#26).

Chacune est servie par une op, ou un paramètre, d'un outil existant de sa famille :
- `GET /api/sirene/info` : `fr_stock_search(op="info")` ;
- `POST /api/sirene/sieges` : `fr_stock_siege(sirens=[…])` ;
- `GET /api/foncier/odre/annees` : `foncier_conso_elec(op="annees")` ;
- `GET /api/reglements` : `urba_reglement(op="liste")` ;
- `GET /api/reglements/{idurba}/raw` : `urba_reglement(op="texte")`, du `text/plain`.

Le banc joue le VRAI outil et le VRAI client de domaine ; seule la couche HTTP est
doublée, et ce qu'elle reçoit (méthode, chemin, paramètres) est ce qu'on vérifie. Les
appels actuels (sans op, avec `siren`, avec `annee`) restent valides.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import oto_mcp.fod.client  # noqa: F401 — cibles du monkeypatch
import oto_mcp.fod.foncier  # noqa: F401
import oto_mcp.fod.http  # noqa: F401
from oto_mcp.mcp_errors import McpError

_ID = "80021_PLU_20251211"


def _tool(module, nom):
    from fastmcp import FastMCP

    m = FastMCP("t")
    module.register(m)
    return asyncio.run(m.get_tool(nom)).fn


def _fr_stock(nom):
    from oto_mcp.tools import fr_stock
    return _tool(fr_stock, nom)


def _foncier(nom):
    from oto_mcp.tools import foncier
    return _tool(foncier, nom)


def _urba():
    from oto_mcp.tools import urba
    return _tool(urba, "urba_reglement")


# --- SIRENE stock ------------------------------------------------------------

def test_info_du_parquet_par_sa_route(monkeypatch):
    vus = []

    def get(path, params=None):
        vus.append((path, params))
        return {"millesime": "2026-11", "age_days": 4}

    monkeypatch.setattr("oto_mcp.fod.client._get", get)
    assert _fr_stock("fr_stock_search")(op="info") == {"millesime": "2026-11", "age_days": 4}
    assert vus == [("/api/sirene/info", None)]


def test_info_refuse_un_filtre(monkeypatch):
    monkeypatch.setattr("oto_mcp.fod.client._get", lambda *a, **k: pytest.fail("appelé"))
    with pytest.raises(McpError, match="naf"):
        _fr_stock("fr_stock_search")(op="info", naf="4711F")


def test_sieges_bruts_en_lot_par_leur_route(monkeypatch):
    vus = []

    def post(path, body=None):
        vus.append((path, body))
        return {"111111111": {"siren": "111111111", "siret": "11111111100015"}}

    monkeypatch.setattr("oto_mcp.fod.client._post", post)
    r = _fr_stock("fr_stock_siege")(sirens=[" 111111111", "222222222"])
    assert r == {"111111111": {"siren": "111111111", "siret": "11111111100015"}}
    assert vus == [("/api/sirene/sieges", {"sirens": ["111111111", "222222222"]})]


def test_un_siege_garde_sa_route(monkeypatch):
    vus = []
    monkeypatch.setattr("oto_mcp.fod.client._get",
                        lambda path, params=None: vus.append(path) or {"siren": "111111111"})
    assert _fr_stock("fr_stock_siege")(siren="111111111") == {"siren": "111111111"}
    assert vus == ["/api/sirene/siege/111111111"]


@pytest.mark.parametrize("kw", [{}, {"siren": "111111111", "sirens": ["222222222"]}])
def test_siren_ou_sirens_exactement_un(kw):
    with pytest.raises(McpError, match="siren_ou_sirens"):
        _fr_stock("fr_stock_siege")(**kw)


# --- foncier : millésimes du transport -----------------------------------------

def test_annees_du_transport_par_leur_route(monkeypatch):
    vus = []

    def get(path, params=None):
        vus.append((path, params))
        return ["2023", "2022"]

    monkeypatch.setattr("oto_mcp.fod.foncier._get", get)
    r = _foncier("foncier_conso_elec")(op="annees")
    assert r == {"reseau": "transport", "annees_disponibles": ["2023", "2022"]}
    assert vus == [("/api/foncier/odre/annees", {})]


def test_annees_refuse_un_autre_parametre():
    with pytest.raises(McpError, match="annee"):
        _foncier("foncier_conso_elec")(op="annees", annee="2023")


def test_conso_exige_toujours_l_annee():
    with pytest.raises(McpError, match="requires annee"):
        _foncier("foncier_conso_elec")(dept="33")


# --- urba : règlements en cache et texte brut ----------------------------------

def test_liste_des_reglements_par_sa_route(monkeypatch):
    vus = []
    corps = {"count": 1, "reglements": [{"idurba": _ID, "titre": "PLU"}]}

    def get(path, params=None, headers=None, timeout=None):
        vus.append((path, params))
        return corps

    monkeypatch.setattr("oto_mcp.fod.http.get", get)
    assert _urba()(op="liste") == corps
    assert vus == [("/api/reglements", None)]


def test_liste_refuse_un_idurba():
    with pytest.raises(McpError, match="does not take idurba"):
        _urba()(op="liste", idurba=_ID)


def _texte(code, texte):
    return httpx.Response(code, text=texte, headers={"content-type": "text/plain"},
                          request=httpx.Request("GET", "http://fod/x"))


def test_texte_brut_lu_en_text_plain(monkeypatch):
    vus = []
    coupe = "x" * 2000 + "\n\n[…tronqué — 5,000,000 caractères au total]"

    def etat(method, path, codes):
        vus.append((method, path, codes))
        return _texte(200, coupe)

    monkeypatch.setattr("oto_mcp.fod.http.etat", etat)
    r = _urba()(op="texte", idurba=_ID, max_chars=2000)
    assert r == {"idurba": _ID, "cached": True, "texte": coupe, "tronque": True}
    assert vus == [("GET", f"/api/reglements/{_ID}/raw?max_chars=2000", (200, 404))]


def test_texte_entier_n_est_pas_tronque(monkeypatch):
    monkeypatch.setattr("oto_mcp.fod.http.etat", lambda *a: _texte(200, "Article UA 1"))
    r = _urba()(op="texte", idurba=_ID)
    assert r["tronque"] is False and r["texte"] == "Article UA 1"


def test_texte_d_un_reglement_non_ingere(monkeypatch):
    monkeypatch.setattr("oto_mcp.fod.http.etat",
                        lambda *a: _texte(404, "règlement non ingéré"))
    r = _urba()(op="texte", idurba=_ID)
    assert r["cached"] is False and "ingest=true" in r["message"]


def test_texte_refuse_les_parametres_des_extraits():
    with pytest.raises(McpError, match="does not take zone"):
        _urba()(op="texte", idurba=_ID, zone="UA")
    with pytest.raises(McpError, match="max_chars must be"):
        _urba()(op="texte", idurba=_ID, max_chars=10)
    with pytest.raises(McpError, match="requires idurba"):
        _urba()(op="texte")


def test_les_extraits_gardent_leur_route(monkeypatch):
    vus = []

    def get(path, params=None, headers=None, timeout=None):
        vus.append(path)
        return {"idurba": _ID, "cached": True, "extraits": []}

    monkeypatch.setattr("oto_mcp.fod.http.get", get)
    assert _urba()(idurba=_ID, zone="UA")["cached"] is True
    assert vus == ["/api/reglement"]

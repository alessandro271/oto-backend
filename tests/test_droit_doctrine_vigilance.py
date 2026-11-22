"""`doctrine_bofip` (BOFiP) et `compliance(op="screen_asset_freeze")` (gels d'avoirs) passent
par le service FOD sans rien calculer : la route appelée, le nom dans le corps, une
référence inconnue nommée, un registre non concluant qui n'est jamais « aucun gel »."""
import asyncio

import httpx
import pytest

import oto_mcp.fod.doctrine  # noqa: F401 — cibles du monkeypatch (import à l'appel)
import oto_mcp.fod.vigilance  # noqa: F401
from oto_mcp.mcp_errors import McpError


def _tool(nom):
    from fastmcp import FastMCP
    from oto_mcp.tools import droit as D

    m = FastMCP("t")
    D.register(m)
    return asyncio.run(m.get_tool(nom)).fn


def test_bofip_recherche_et_lecture_par_les_routes_fod(monkeypatch):
    vus = []

    def get(path, params=None):
        vus.append((path, params))
        return None if path.endswith("INCONNU") else {"identifiant_juridique": "x"}

    monkeypatch.setattr("oto_mcp.fod.doctrine._get", get)
    t = _tool("doctrine_bofip")
    t(op="search", query="pacte Dutreil")
    t(op="get", identifiant="BOI-ENR-DMTG-20-30-20-20", max_chars=0)
    assert vus == [("/api/doctrine/bofip", {"query": "pacte Dutreil", "limit": 6}),
                   ("/api/doctrine/bofip/BOI-ENR-DMTG-20-30-20-20", {"max_chars": 0})]
    with pytest.raises(McpError, match="No BOFiP publication"):
        t(op="get", identifiant="BOI-INCONNU")
    with pytest.raises(McpError, match="requires query"):
        t(op="search")


def _reponse(code, corps):
    return httpx.Response(code, json=corps, request=httpx.Request("POST", "http://fod/x"))


def test_gel_d_avoirs_nom_dans_le_corps(monkeypatch):
    vu = {}

    def etat(method, path, codes, body=None):
        vu.update(method=method, path=path, body=body)
        return _reponse(200, {"total": 0, "correspondances": []})

    monkeypatch.setattr("oto_mcp.fod.vigilance._etat", etat)
    r = _tool("compliance")(op="screen_asset_freeze", nom="Martin", prenom="Jean")
    assert r["total"] == 0
    assert vu == {"method": "POST", "path": "/api/vigilance/gels-avoirs/controle",
                  "body": {"nom": "Martin", "limit": 10, "prenom": "Jean"}}


def test_registre_non_concluant_n_est_jamais_aucun_gel(monkeypatch):
    monkeypatch.setattr("oto_mcp.fod.vigilance._etat",
                        lambda *a, **k: _reponse(503, {"detail": "registre tronqué"}))
    with pytest.raises(McpError, match="PAS une absence de correspondance"):
        _tool("compliance")(op="screen_asset_freeze", nom="Martin")


def test_chaque_op_exige_ses_champs():
    t = _tool("compliance")
    with pytest.raises(McpError, match="requires nom"):
        t(op="screen_asset_freeze")
    with pytest.raises(McpError, match="requires date_entree"):
        t(op="check_mutual_termination")

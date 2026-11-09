"""`pennylane_bank_accounts` : les comptes bancaires et leur solde courant.

Ce qui casse en silence côté agent : un solde absent lu comme zéro (un cumul
faux sans erreur), et une banque non nommée faute de droit sur la clé.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from oto.tools.common.errors import UpstreamHTTPError
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _outil():
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane

    m = FastMCP("t")
    pennylane.register(m)
    return asyncio.run(m.get_tool("pennylane_bank_accounts")).fn


def _compte(id_, solde, banque=9):
    return {"id": id_, "name": f"Compte {id_}", "currency": "EUR", "balance": solde,
            "bank_establishment": {"id": banque}, "journal": None,
            "ledger_account": {"id": 1, "url": "u"},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-11-08T06:00:00Z"}


def _pages(comptes, banques):
    def fetch_all_pages(endpoint, params=None, **kw):
        if endpoint == "bank_accounts":
            return comptes
        assert endpoint == "bank_establishments"
        filtre = json.loads(params["filter"])
        assert filtre == [{"field": "id", "operator": "in", "value": [9]}]
        if isinstance(banques, Exception):
            raise banques
        return banques
    return fetch_all_pages


def test_rend_solde_banque_devise_et_date(client):
    client.fetch_all_pages.side_effect = _pages(
        [_compte(1, "1234.56"), _compte(2, "-10.00")],
        [{"id": 9, "name": "Banque test"}])
    res = _outil()()
    assert res == {"bank_accounts": [
        {"id": 1, "name": "Compte 1", "currency": "EUR", "balance": "1234.56",
         "bank": {"id": 9, "name": "Banque test"}, "updated_at": "2026-11-08T06:00:00Z"},
        {"id": 2, "name": "Compte 2", "currency": "EUR", "balance": "-10.00",
         "bank": {"id": 9, "name": "Banque test"}, "updated_at": "2026-11-08T06:00:00Z"},
    ]}


def test_un_solde_nul_reste_un_solde(client):
    client.fetch_all_pages.side_effect = _pages(
        [_compte(1, "0.00")], [{"id": 9, "name": "B"}])
    assert _outil()()["bank_accounts"][0]["balance"] == "0.00"


@pytest.mark.parametrize("solde", [None, ""])
def test_solde_absent_leve_au_lieu_de_zero(client, solde):
    client.fetch_all_pages.side_effect = _pages(
        [_compte(1, "5.00"), _compte(2, solde)], [])
    with pytest.raises(McpError, match=r"WITHOUT a balance \(ids \[2\]\)"):
        _outil()()


def test_droit_banques_manquant_le_dit(client):
    client.fetch_all_pages.side_effect = _pages(
        [_compte(1, "5.00")], UpstreamHTTPError(403, {"error": "scope"}, service="pennylane"))
    with pytest.raises(McpError, match="bank_establishments:readonly.*bank_names=False"):
        _outil()()


def test_sans_noms_de_banque_ne_lit_que_les_comptes(client):
    client.fetch_all_pages.side_effect = _pages([_compte(1, "5.00")], [])
    res = _outil()(bank_names=False)
    assert res["bank_accounts"][0]["bank"] == {"id": 9, "name": None}
    assert res["bank_accounts"][0]["balance"] == "5.00"
    client.fetch_all_pages.assert_called_once_with("bank_accounts")


def test_autre_refus_remonte_tel_quel(client):
    client.fetch_all_pages.side_effect = _pages(
        [_compte(1, "5.00")], UpstreamHTTPError(500, "boom", service="pennylane"))
    with pytest.raises(UpstreamHTTPError):
        _outil()()


def test_aucun_compte(client):
    client.fetch_all_pages.side_effect = _pages([], [])
    assert _outil()() == {"bank_accounts": []}
    client.fetch_all_pages.assert_called_once_with("bank_accounts")

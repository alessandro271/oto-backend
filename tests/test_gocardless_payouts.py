"""Outils GoCardless des versements groupés (payouts) — otomata-tech/oto#250.

`gocardless_payouts` rend un dict aux clés nommées (pas une liste nue, cf.
`test_un_seul_canal`) et resserre son défaut (cf. `test_sorties_listes_projetees`) ;
`gocardless_payout` rend le versement et toutes ses lignes, tels que le client
les lit.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp.tools import gocardless as G

_PAYOUT = {
    "id": "PO1", "amount": 98000, "deducted_fees": 2000, "currency": "EUR",
    "status": "paid", "arrival_date": "2026-08-03", "created_at": "2026-08-01T10:00:00Z",
    "reference": "REF-1", "payout_type": "merchant", "tax_currency": None,
    "fx": {"exchange_rate": None}, "metadata": {}, "links": {"creditor": "CR1"},
}


class _FauxClient:
    def __init__(self):
        self.appels = []

    def list_payouts(self, **kw):
        self.appels.append(("list_payouts", kw))
        return [dict(_PAYOUT)]

    def payout_detail(self, payout_id):
        self.appels.append(("payout_detail", payout_id))
        return {"payout": dict(_PAYOUT),
                "items": [{"type": "payment_paid_out", "amount": 100000,
                           "links": {"payment": "PM1"}},
                          {"type": "gocardless_fee", "amount": -2000,
                           "links": {"payment": "PM1"}}]}


@pytest.fixture
def outils(monkeypatch):
    import oto.tools.gocardless as pkg

    cli = _FauxClient()
    monkeypatch.setattr(pkg, "GoCardlessClient", lambda **kw: cli)
    monkeypatch.setattr(G.access, "resolve_api_key", lambda slug: ("k", False))
    m = FastMCP("t")
    G.register(m)

    def outil(nom):
        return asyncio.run(m.get_tool(nom)).fn

    return cli, outil


def test_la_liste_transmet_les_filtres_et_resserre_le_defaut(outils):
    cli, outil = outils
    rendu = outil("gocardless_payouts")(status="paid", currency="EUR",
                                        since="2026-08-01", until="2026-09-01")
    assert cli.appels == [("list_payouts", {
        "status": "paid", "limit": 50, "currency": "EUR", "reference": None,
        "created_gt": "2026-08-01", "created_lt": "2026-09-01"})]
    assert rendu == {"payouts": [{k: _PAYOUT[k] for k in G._PAYOUT_KEYS}]}
    assert "fx" not in rendu["payouts"][0] and "links" not in rendu["payouts"][0]


def test_full_rend_l_objet_brut(outils):
    _, outil = outils
    assert outil("gocardless_payouts")(full=True) == {"payouts": [_PAYOUT]}


def test_le_detail_rend_le_versement_et_ses_lignes(outils):
    cli, outil = outils
    rendu = outil("gocardless_payout")("PO1")
    assert cli.appels == [("payout_detail", "PO1")]
    assert rendu["payout"]["id"] == "PO1"
    assert sum(i["amount"] for i in rendu["items"]) == rendu["payout"]["amount"]

"""PayFit — les heures sup d'un bulletin, et rien d'autre.

L'API ne sert aucune ligne de bulletin ; `payfit_payslip(op="overtime")` lit le PDF
CÔTÉ SERVEUR et n'en rend que les lignes qui nomment des heures sup. Ce fichier
verrouille : le tri paiement / allègement, la lecture des nombres français, le fait
qu'aucune autre ligne du bulletin (NIR, IBAN, salaire de base) ne sort, le filtre du
mois, et l'absence de verrou documents (le bulletin ne quitte jamais le serveur).

Toutes les valeurs sont factices.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from _pdf_texte import pdf_sans_texte, pdf_texte
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import payfit_bulletin as pb

K = "000000000000000000000a0a"
BULLETIN = [
    "Salaire de base 151,67 12,50 1 895,88",
    "Heures supplémentaires 25% 8,00 15,63 125,04",
    "Heures sup. majorées 50 % 2,00 18,75 37,50",
    "Réduction salariale heures supplémentaires -14,25",
    "Heures complémentaires 10 % 4,00 13,75 55,00",
    "N° de sécurité sociale 1 85 05 75 000 000 00",
    "IBAN FR76 0000 0000 0000 0000 0000 000",
]


# ── Le parseur ────────────────────────────────────────────────────────────────

def test_only_overtime_lines_come_out():
    lignes = pb.overtime_lines("\n".join(BULLETIN))
    assert [l["label"] for l in lignes] == [
        "Heures supplémentaires", "Heures sup. majorées",
        "Réduction salariale heures supplémentaires", "Heures complémentaires"]
    texte = " ".join(l["line"] for l in lignes)
    assert "sécurité sociale" not in texte and "IBAN" not in texte
    assert "Salaire de base" not in texte


def test_numbers_and_rates_are_read_the_french_way():
    hs = pb.overtime_lines("Heures supplémentaires 25% 8,00 15,63 1 125,04")[0]
    assert hs["rates"] == [25.0]
    assert hs["numbers"] == [8.0, 15.63, 1125.04]


def test_a_contribution_relief_is_never_a_payment():
    kinds = {l["label"]: l["kind"] for l in pb.overtime_lines("\n".join(BULLETIN))}
    assert kinds["Réduction salariale heures supplémentaires"] == pb.ALLEGEMENT
    assert kinds["Heures supplémentaires"] == pb.PAIEMENT


@pytest.mark.parametrize("ligne", ["HS 25 8,00 15,63 125,04", "H. sup 3,00 45,00",
                                   "HEURES SUPPLEMENTAIRES 1,00 15,00"])
def test_short_and_unaccented_labels_are_found(ligne):
    assert len(pb.overtime_lines(ligne)) == 1


def test_nothing_to_read_is_an_empty_list():
    assert pb.overtime_lines("") == []
    assert pb.overtime_lines("Salaire de base 151,67") == []


# ── L'op, de bout en bout (PDF réel fabriqué, client simulé) ──────────────────

@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.payfit.PayfitClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("pf-test", False))
    inst.list_payslips.return_value = {"payslips": [
        {"year": 2026, "month": 1, "contractId": "c1", "payslipId": "p1"},
        {"year": 2026, "month": 2, "contractId": "c1", "payslipId": "p2"},
    ]}
    inst.get_payslip.return_value = {"data": pdf_texte(BULLETIN),
                                     "filename": "bulletin.pdf",
                                     "mimetype": "application/pdf"}
    return inst


def _payslip():
    from fastmcp import FastMCP

    from oto_mcp.tools import payfit_paie as PP
    m = FastMCP("t")
    PP.register(m)
    return asyncio.run(m.get_tool("payfit_payslip")).fn


def test_overtime_reads_one_month_and_returns_ids_not_the_payslip(client):
    out = _payslip()(op="overtime", collaborator_id=K, date="202602")
    client.get_payslip.assert_called_once_with(K, "c1", "p2")
    assert out["count"] == 1
    slip = out["payslips"][0]
    assert (slip["year"], slip["month"], slip["payslipId"]) == (2026, 2, "p2")
    assert len(slip["lines"]) == 4
    assert "sécurité sociale" not in str(out) and "IBAN" not in str(out)


def test_overtime_without_date_reads_the_most_recent_first(client):
    out = _payslip()(op="overtime", collaborator_id=K)
    assert [p["payslipId"] for p in out["payslips"]] == ["p2", "p1"]


def test_overtime_is_not_behind_the_documents_lock(client, monkeypatch):
    # Le verrou garde les documents qui SORTENT ; celui-ci ne sort pas.
    from oto_mcp.tools import payfit_garde
    monkeypatch.setattr(payfit_garde, "documents_open", lambda: False)
    out = _payslip()(op="overtime", collaborator_id=K, date="202601")
    assert out["payslips"][0]["lines"]


def test_an_unreadable_payslip_says_why(client):
    client.get_payslip.return_value = {"data": pdf_sans_texte(),
                                       "filename": "scan.pdf",
                                       "mimetype": "application/pdf"}
    slip = _payslip()(op="overtime", collaborator_id=K, date="202601")["payslips"][0]
    assert slip["lines"] == [] and slip["unreadable"].startswith("empty")


@pytest.mark.parametrize("date", ["2026-01", "202613", "2026"])
def test_a_malformed_month_is_refused_before_the_network(client, date):
    with pytest.raises(McpError, match="AAAAMM"):
        _payslip()(op="overtime", collaborator_id=K, date=date)
    client.list_payslips.assert_not_called()


@pytest.mark.parametrize("kwargs,champ", [
    ({"contract_id": "c1"}, "`contract_id`"), ({"payslip_id": "p1"}, "`payslip_id`"),
    ({"fields": ["year"]}, "`fields`")])
def test_overtime_refuses_what_it_does_not_use(client, kwargs, champ):
    with pytest.raises(McpError, match=champ):
        _payslip()(op="overtime", collaborator_id=K, **kwargs)


def test_date_is_refused_on_the_other_ops(client):
    with pytest.raises(McpError, match="`date`"):
        _payslip()(collaborator_id=K, date="202601")

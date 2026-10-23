"""PayFit — le brut par contrat actif, cadre / non-cadre (`payfit_payroll(op=
"gross_by_contract")`, `payfit_brut`).

Ce que ce fichier verrouille :
- le brut = débit − crédit des SEULS comptes 641x, ventilé par compte à quatre
  chiffres ; un 645x (cotisations) n'y entre jamais ;
- une ligne par contrat ACTIF ; le brut d'un contrat sorti va dans `others` ;
  un contrat actif sans écriture rend `gross: null`, jamais 0 ;
- le collège lu dans le statut conventionnel DSN (S21.G00.40.002 : 03-04 cadre,
  05-07 non-cadre, le reste `null`) ;
- la période (douze mois au plus, `YYYYMM`), les mois de paie non clos signalés ;
- l'outil déclaré en lecture (une recette peut l'appeler) et les scopes déclarés.

Toutes les valeurs sont factices.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import payfit_brut as B


def _e(account, debit=None, credit=None, contract="k1"):
    return {"accountId": account, "debit": debit, "credit": credit, "contractId": contract}


def _c(cid, statut="06", status="ACTIVE", last="Martin", **kw):
    return {"contractId": cid, "firstName": "Ada", "lastName": last, "status": status,
            "jobName": "Poste", "startDate": "2024-01-01", "natureContratDsn": "01",
            "statutConventionnelDsn": statut, "estCadreDirigeant": False, **kw}


def test_le_brut_ne_somme_que_les_comptes_641_debit_moins_credit():
    out = B.gross_by_contract({"202609": [
        _e("6411000", 2000), _e("6412000", 150.5), _e("6413000", 300),
        _e("6411000", credit=100),                 # retenue d'absence
        _e("6451000", 800), _e("4210000", credit=1900)]}, [_c("k1")])
    row = out["contracts"][0]
    assert row["gross"] == 2350.5
    assert row["by_account"] == {"6411": 1900.0, "6412": 150.5, "6413": 300.0}
    assert row["months_paid"] == 1


def test_une_ligne_par_contrat_actif_le_sorti_va_dans_others():
    out = B.gross_by_contract({"202609": [
        _e("6411000", 1000, contract="k1"), _e("6411000", 500, contract="parti"),
        _e("6411000", 40, contract=None)]},
        [_c("k1"), _c("parti", status="ARCHIVED"), _c("neuf", last="Abel")])
    assert [r["contractId"] for r in out["contracts"]] == ["neuf", "k1"]   # par nom
    assert out["contracts"][0]["gross"] is None                 # un manque, pas un zéro
    assert out["contracts"][0]["by_account"] == {}
    assert out["others"] == {"contracts": 1, "gross": 540.0}


@pytest.mark.parametrize("statut,dirigeant,attendu", [
    ("01", False, None), ("02", False, None), ("03", False, "cadre"),
    ("04", False, "cadre"), ("05", False, "non_cadre"), ("06", False, "non_cadre"),
    ("07", False, "non_cadre"), ("08", False, None), ("10", False, None),
    (None, False, None), ("06", True, "cadre")])
def test_le_college_vient_du_statut_conventionnel_dsn(statut, dirigeant, attendu):
    assert B.college(_c("k", statut=statut, estCadreDirigeant=dirigeant)) == attendu


def test_plusieurs_mois_s_additionnent_et_disent_combien_ont_paye():
    out = B.gross_by_contract({"202608": [_e("6411000", 1000)],
                               "202609": [_e("6411000", "1000.25")]}, [_c("k1")])
    assert out["contracts"][0]["gross"] == 2000.25
    assert out["contracts"][0]["months_paid"] == 2


def test_un_montant_illisible_est_refuse_en_le_nommant():
    with pytest.raises(ValueError, match="6411000"):
        B.gross_by_contract({"202609": [_e("6411000", "beaucoup")]}, [_c("k1")])


def test_la_periode():
    assert B.months("202609", None) == ["202609"]
    assert B.months("202511", "202602") == ["202511", "202512", "202601", "202602"]
    assert len(B.months("202501", "202512")) == 12
    with pytest.raises(ValueError, match="at most 12"):
        B.months("202501", "202601")
    with pytest.raises(ValueError, match="before"):
        B.months("202609", "202608")
    with pytest.raises(ValueError, match="YYYYMM"):
        B.months("2026-09", None)


# --- l'outil ------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.payfit.PayfitClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("pf-test", False))
    return inst


def _tool():
    from fastmcp import FastMCP

    from oto_mcp.tools import payfit_paie as PP

    m = FastMCP("t")
    PP.register(m)
    return asyncio.run(m.get_tool("payfit_payroll"))


def test_l_outil_rend_le_compact_et_les_mois_non_clos(client):
    client.get_company.return_value = {"name": "Société factice", "siren": "000000000",
                                       "siret": "00000000000000", "address": "x"}
    client.get_payroll_status.side_effect = lambda m: {
        "status": "completed" if m == "202608" else "not_completed"}
    client.list_accounting_entries.side_effect = lambda m: [_e("6411000", 1000)]
    client.list_contracts.side_effect = [
        {"contracts": [_c("k1")], "meta": {"nextPageToken": "p2"}},
        {"contracts": [_c("k2", statut="04", last="Zola")], "meta": {}}]
    out = _tool().fn(op="gross_by_contract", date="202608", date_to="202609")
    assert out["company"] == {"name": "Société factice", "siren": "000000000",
                              "siret": "00000000000000"}
    assert out["period"] == {"from": "202608", "to": "202609"}
    assert out["months_not_completed"] == ["202609"]
    assert [(r["contractId"], r["college"], r["gross"]) for r in out["contracts"]] == [
        ("k1", "non_cadre", 2000.0), ("k2", "cadre", None)]
    client.get_company.assert_called_once_with(fr=True)
    assert client.list_contracts.call_args_list[1].kwargs == {
        "limit": 50, "cursor": "p2", "fr": True}


def test_l_outil_refuse_ce_qui_ne_sert_pas(client):
    with pytest.raises(McpError, match="fields"):
        _tool().fn(op="gross_by_contract", date="202609", fields=["accountId"])
    with pytest.raises(McpError, match="date_to"):
        _tool().fn(op="accounting", date="202609", date_to="202610")
    with pytest.raises(McpError, match="at most 12"):
        _tool().fn(op="gross_by_contract", date="202501", date_to="202601")
    client.list_accounting_entries.assert_not_called()


def test_l_outil_est_declare_en_lecture_et_ses_scopes_sont_declares():
    from oto_mcp.providers.payfit import ACCESS_LEVELS
    from oto_mcp.tools.lecture import en_lecture

    assert en_lecture(_tool())
    assert ACCESS_LEVELS["tools"]["payfit_payroll"]["gross_by_contract"] == (
        "accounting:read", "contracts:read")

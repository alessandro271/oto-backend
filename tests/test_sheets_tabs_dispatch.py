"""Dispatch des ops d'onglets, de lignes et de formats de `sheets_spreadsheet`
(signaux oto 1386/1384/1376), et `sheets_create(sheets=…)`.

Ce que ce fichier verrouille : chaque op atteint SA méthode client et aucune autre ;
`delete_sheet` et `delete_rows` sont en ESSAI À BLANC par défaut (rien supprimé, le
retour dit ce qui partirait) et ne suppriment qu'avec `dry_run=False` ; les arguments
obligatoires par op ; un refus du client (onglet inconnu) devient une erreur lisible.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError

DESTRUCTIVE = ("delete_sheet", "delete_rows", "write", "clear", "append")


def _call(name: str, **kwargs):
    from fastmcp import FastMCP
    from oto_mcp.tools import sheets as S

    m = FastMCP("t")
    S.register(m)
    return asyncio.run(asyncio.run(m.get_tool(name)).fn(**kwargs))


def _raises(name: str, **kwargs):
    with pytest.raises(McpError) as e:
        _call(name, **kwargs)
    return str(e.value)


@pytest.fixture
def client(monkeypatch):
    from oto_mcp.tools import sheets as S

    inst = MagicMock()
    inst.sheet_id.return_value = 7
    monkeypatch.setattr(S, "_client_for_user", lambda account=None: inst)
    return inst


def _untouched(client, *methods):
    for m in methods:
        getattr(client, m).assert_not_called()


def test_create_transmet_les_onglets_nommes(client):
    client.create.return_value = {"id": "sid", "sheets": ["Hypothèses", "P&L"]}
    out = _call("sheets_create", title="BP", sheets=["Hypothèses", "P&L"])
    client.create.assert_called_once_with("BP", ["Hypothèses", "P&L"])
    assert out["sheets"] == ["Hypothèses", "P&L"]


def test_add_sheet_et_rename_sheet_atteignent_leur_methode(client):
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="add_sheet", title="Q4", index=2)
    client.add_sheet.assert_called_once_with("sid", "Q4", 2)
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="rename_sheet", sheet="Feuille 1",
          title="Synthèse")
    client.rename_sheet.assert_called_once_with("sid", "Feuille 1", "Synthèse")
    _untouched(client, *DESTRUCTIVE)


def test_number_format_atteint_sa_methode_avec_le_type(client):
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="number_format",
          range="'P&L'!B2:D", pattern="#,##0.00", type="CURRENCY")
    client.number_format.assert_called_once_with("sid", "'P&L'!B2:D", "#,##0.00", "CURRENCY")
    _untouched(client, *DESTRUCTIVE)


def test_copy_sheet_atteint_sa_methode_avec_la_destination(client):
    _call("sheets_spreadsheet", spreadsheet_id="modele", op="copy_sheet", sheet="Octobre",
          to_spreadsheet_id="produit", title="Suivi", index=0)
    client.copy_sheet.assert_called_once_with("modele", "Octobre", "produit", "Suivi", 0)
    _untouched(client, *DESTRUCTIVE)


def test_les_ops_de_mise_en_forme_atteignent_leur_methode(client):
    style = {"background": "#d9d9d9", "wrap": True}
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="format", range="A1:Z1", style=style)
    client.format.assert_called_once_with("sid", "A1:Z1", style)
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="column_width", range="B:D", width=140)
    client.column_width.assert_called_once_with("sid", "B:D", 140)
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="freeze", sheet="Suivi", frozen_rows=1)
    client.freeze.assert_called_once_with("sid", "Suivi", 1, None)
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="dropdown", range="F2:F",
          options=["Payé", "À relancer"])
    client.dropdown.assert_called_once_with("sid", "F2:F", ["Payé", "À relancer"])
    _untouched(client, *DESTRUCTIVE)


def test_un_style_refuse_par_le_client_est_une_erreur_lisible(client):
    from oto.tools.google.sheets.lib.sheets_client import SheetsClientError

    client.format.side_effect = SheetsClientError("style: unknown key(s) fond")
    msg = _raises("sheets_spreadsheet", spreadsheet_id="sid", op="format", range="A1",
                  style={"fond": "#fff"})
    assert "unknown key(s) fond" in msg


@pytest.mark.parametrize("op,kwargs,missing", [
    ("copy_sheet", {}, "sheet"),
    ("format", {"range": "A1"}, "style"),
    ("format", {"style": {"bold": True}}, "range"),
    ("column_width", {"range": "B:D"}, "width"),
    ("freeze", {"frozen_rows": 1}, "sheet"),
    ("freeze", {"sheet": "A"}, "frozen_rows"),
    ("dropdown", {"range": "F2:F"}, "options"),
    ("add_sheet", {}, "title"),
    ("rename_sheet", {"sheet": "A"}, "title"),
    ("rename_sheet", {"title": "B"}, "sheet"),
    ("delete_sheet", {}, "sheet"),
    ("delete_rows", {"sheet": "A", "end_row": 3}, "start_row"),
    ("number_format", {"range": "A1"}, "pattern"),
    ("number_format", {"pattern": "0"}, "range"),
])
def test_un_argument_obligatoire_absent_est_refuse_sans_toucher_le_client(client, op, kwargs, missing):
    msg = _raises("sheets_spreadsheet", spreadsheet_id="sid", op=op, **kwargs)
    assert missing in msg and op in msg
    for m in ("add_sheet", "rename_sheet", "delete_sheet", "delete_rows", "number_format",
              "copy_sheet", "format", "column_width", "freeze", "dropdown"):
        getattr(client, m).assert_not_called()


# --- essai à blanc par défaut ---------------------------------------------------------

def test_delete_sheet_est_un_essai_a_blanc_par_defaut(client):
    out = _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_sheet", sheet="Brouillon")
    assert out["dry_run"] is True
    assert out["would_delete"] == {"tab": "Brouillon", "id": 7, "everything_in_it": True}
    client.sheet_id.assert_called_once_with("sid", "Brouillon")
    _untouched(client, *DESTRUCTIVE)


def test_delete_sheet_supprime_seulement_avec_dry_run_false(client):
    client.delete_sheet.return_value = {"id": 7, "deleted": True}
    out = _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_sheet",
                sheet="Brouillon", dry_run=False)
    client.delete_sheet.assert_called_once_with("sid", "Brouillon")
    assert out == {"id": 7, "deleted": True}


def test_delete_rows_est_un_essai_a_blanc_par_defaut(client):
    out = _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_rows", sheet="Leads",
                start_row=5, end_row=9)
    assert out["dry_run"] is True
    assert out["would_delete"] == {"tab": "Leads", "id": 7, "from_row": 5, "to_row": 9, "rows": 5}
    _untouched(client, *DESTRUCTIVE)


def test_delete_rows_supprime_seulement_avec_dry_run_false(client):
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_rows", sheet="Leads",
          start_row=5, end_row=9, dry_run=False)
    client.delete_rows.assert_called_once_with("sid", "Leads", 5, 9)


def test_des_bornes_incoherentes_sont_refusees_a_blanc_sans_appel(client):
    msg = _raises("sheets_spreadsheet", spreadsheet_id="sid", op="delete_rows", sheet="Leads",
                  start_row=9, end_row=5)
    assert "start_row >= 1" in msg
    client.delete_rows.assert_not_called()


def test_un_onglet_inconnu_est_une_erreur_lisible(client):
    from oto.tools.google.sheets.lib.sheets_client import SheetsClientError

    client.sheet_id.side_effect = SheetsClientError("no tab named 'X'; tabs: 'Feuille 1'")
    msg = _raises("sheets_spreadsheet", spreadsheet_id="sid", op="delete_sheet", sheet="X")
    assert "no tab named 'X'" in msg and "'Feuille 1'" in msg

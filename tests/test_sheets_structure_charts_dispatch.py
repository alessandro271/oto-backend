"""Dispatch des ops de structure, de données et de mise en forme avancée de
`sheets_spreadsheet`, et de `sheets_chart`.

Ce que ce fichier verrouille : chaque op atteint SA méthode client avec ses arguments
dans l'ordre ; `delete_columns` est en ESSAI À BLANC par défaut (bornes vérifiées, rien
supprimé) ; un argument obligatoire manquant est refusé avant tout appel ; un refus du
client devient une erreur lisible.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


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


R = "'Suivi'!A2:F"


@pytest.mark.parametrize("kwargs, method, args", [
    ({"op": "insert_rows", "sheet": "Suivi", "start_row": 5, "count": 2},
     "insert_rows", ("Suivi", 5, 2)),
    ({"op": "insert_columns", "sheet": "Suivi", "start_column": "C", "count": 1},
     "insert_columns", ("Suivi", "C", 1)),
    ({"op": "hide", "range": "'Suivi'!C:E"}, "set_hidden", ("'Suivi'!C:E", True)),
    ({"op": "show", "range": "'Suivi'!C:E"}, "set_hidden", ("'Suivi'!C:E", False)),
    ({"op": "auto_fit", "range": R}, "auto_fit", (R,)),
    ({"op": "merge", "range": R}, "merge", (R,)),
    ({"op": "unmerge", "range": R}, "unmerge", (R,)),
    ({"op": "conditional_format", "range": R, "rule": {"when": "blank"}},
     "conditional_format", (R, {"when": "blank"})),
    ({"op": "clear_conditional_formats", "sheet": "Suivi"}, "clear_conditional_formats",
     ("Suivi",)),
    ({"op": "sort", "range": R, "sort_by": [{"column": "C"}]}, "sort", (R, [{"column": "C"}])),
    ({"op": "filter", "range": R}, "set_filter", (R,)),
    ({"op": "clear_filter", "sheet": "Suivi"}, "clear_filter", ("Suivi",)),
    ({"op": "find_replace", "find": "a", "replacement": "b"}, "find_replace",
     ("a", "b", None, False, False)),
    ({"op": "find_replace", "find": "a", "replacement": "", "range": R, "match_case": True,
      "whole_cell": True}, "find_replace", ("a", "", R, True, True)),
    ({"op": "fill", "range": "'Suivi'!F2:F120", "value": "=B2*C2"}, "fill",
     ("'Suivi'!F2:F120", "=B2*C2")),
    ({"op": "fill", "range": "'Suivi'!G2:G9", "value": 0}, "fill", ("'Suivi'!G2:G9", 0)),
    ({"op": "format", "range": R, "style": {"borders": "all"}}, "format",
     (R, {"borders": "all"})),
    # rows of the table that were branches before: same arguments as then
    ({"op": "add_sheet", "title": "Q4"}, "add_sheet", ("Q4", None)),
    ({"op": "number_format", "range": R, "pattern": "0%"}, "number_format", (R, "0%", "NUMBER")),
    ({"op": "copy_sheet", "sheet": "Modèle"}, "copy_sheet", ("Modèle", None, None, None)),
])
def test_chaque_op_atteint_sa_methode(client, kwargs, method, args):
    _call("sheets_spreadsheet", spreadsheet_id="sid", **kwargs)
    getattr(client, method).assert_called_once_with("sid", *args)


@pytest.mark.parametrize("kwargs, missing", [
    ({"op": "insert_rows", "sheet": "Suivi", "start_row": 5}, "count"),
    ({"op": "insert_columns", "sheet": "Suivi", "count": 1}, "start_column"),
    ({"op": "hide"}, "range"),
    ({"op": "sort", "range": R}, "sort_by"),
    ({"op": "conditional_format", "range": R}, "rule"),
    ({"op": "find_replace", "find": "a"}, "replacement"),
    ({"op": "clear_filter"}, "sheet"),
    ({"op": "fill", "range": R}, "value"),
    ({"op": "delete_columns", "sheet": "Suivi", "start_column": "C"}, "end_column"),
])
def test_argument_obligatoire_refuse(client, kwargs, missing):
    assert f"requires {missing}" in _raises("sheets_spreadsheet", spreadsheet_id="sid", **kwargs)


def test_delete_columns_a_blanc_par_defaut(client):
    out = _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_columns", sheet="Suivi",
                start_column="c", end_column="E")
    assert out["dry_run"] is True
    assert out["would_delete"] == {"tab": "Suivi", "id": 7, "from_column": "C",
                                   "to_column": "E", "columns": 3}
    client.delete_columns.assert_not_called()


@pytest.mark.parametrize("start, end", [("E", "C"), ("C3", "E")])
def test_delete_columns_a_blanc_verifie_les_bornes(client, start, end):
    _raises("sheets_spreadsheet", spreadsheet_id="sid", op="delete_columns", sheet="Suivi",
            start_column=start, end_column=end)
    client.delete_columns.assert_not_called()


def test_delete_columns_supprime_avec_dry_run_false(client):
    _call("sheets_spreadsheet", spreadsheet_id="sid", op="delete_columns", sheet="Suivi",
          start_column="C", end_column="E", dry_run=False)
    client.delete_columns.assert_called_once_with("sid", "Suivi", "C", "E")


def test_un_refus_du_client_devient_une_erreur_lisible(client):
    from oto.tools.google.sheets.lib.sheets_client import SheetsClientError
    client.sort.side_effect = SheetsClientError("sort_by: column 'H' is outside")
    assert "outside" in _raises("sheets_spreadsheet", spreadsheet_id="sid", op="sort", range=R,
                                sort_by=[{"column": "H"}])


# --- sheets_chart ----------------------------------------------------------------------

def test_chart_list_par_defaut(client):
    client.list_charts.return_value = [{"id": 1}]
    assert _call("sheets_chart", spreadsheet_id="sid") == {"charts": [{"id": 1}], "count": 1}


def test_chart_add_transmet_tout(client):
    _call("sheets_chart", spreadsheet_id="sid", op="add", range="'Suivi'!A1:C13", type="pie",
          title="T", anchor="'Synthèse'!H2")
    client.add_chart.assert_called_once_with("sid", "'Suivi'!A1:C13", "pie", "T",
                                             "'Synthèse'!H2", False)


def test_chart_add_exige_range_et_delete_exige_chart_id(client):
    assert "requires range" in _raises("sheets_chart", spreadsheet_id="sid", op="add")
    assert "requires chart_id" in _raises("sheets_chart", spreadsheet_id="sid", op="delete")
    client.add_chart.assert_not_called()
    client.delete_chart.assert_not_called()


def test_chart_delete(client):
    _call("sheets_chart", spreadsheet_id="sid", op="delete", chart_id=7)
    client.delete_chart.assert_called_once_with("sid", 7)


def test_chart_op_inconnue_refusee_avant_le_client(monkeypatch):
    from oto_mcp.tools import sheets as S
    monkeypatch.setattr(S, "_client_for_user", lambda account=None: pytest.fail("client built"))
    assert "op must be" in _raises("sheets_chart", spreadsheet_id="sid", op="move")


def test_lecture_vide_et_classeur_sans_graphique_ont_leur_phrase():
    from oto_mcp.redaction import EMPTY_MESSAGES

    assert "0 ligne" in EMPTY_MESSAGES["sheets_spreadsheet"]
    assert "graphique" in EMPTY_MESSAGES["sheets_chart"]

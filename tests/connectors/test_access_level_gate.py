"""Où un niveau d'accès MORD — garde d'appel, outils masqués, ops servies. Bancs SANS base.

Le contexte de l'appel (sub, org, équipe active) et le réglage sont doublés ; la lecture
réelle et le chemin servi sont éprouvés par `test_access_level_live.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import access, call_axes
from oto_mcp.connectors import access_level_gate as gate
from oto_mcp.connectors import access_levels as al
from oto_mcp.mcp_errors import McpError

ORG, EQUIPE = 11, 22
SCHEMA = {"type": "object", "properties": {
    "module": {"type": "string"},
    "op": {"default": "list", "enum": ["list", "get", "search", "create", "update",
                                       "delete"], "type": "string"}},
    "required": ["module"]}


@pytest.fixture
def contexte(monkeypatch):
    """Un sub dans l'org ORG, équipe active EQUIPE ; `regler(...)` pose le réglage lu."""
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "sub-x")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: EQUIPE)
    lus = []

    def regler(**par_connecteur):
        def settings(org, group):
            lus.append((org, group))
            return par_connecteur
        monkeypatch.setattr(al, "settings", settings)
    regler()
    return regler, lus


async def _schema():
    return SCHEMA


def _appel(tool: str, args: dict):
    return asyncio.run(gate.require_covered(tool, args, _schema))


def test_au_niveau_full_tout_passe(contexte):
    _appel("zoho_record", {"module": "Leads", "op": "delete"})
    _appel("zohodesk_create_ticket", {"data": {}})


def test_un_outil_sans_niveaux_ne_lit_rien(contexte):
    _regler, lus = contexte
    _appel("serper_search", {"q": "x"})
    assert lus == [], "un connecteur sans niveaux ne coûte aucune lecture"


def test_sous_read_l_ecriture_est_refusee_nommement(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    with pytest.raises(McpError) as e:
        _appel("zoho_record", {"module": "Leads", "op": "create"})
    data = e.value.error.data
    assert data["code"] == gate.CODE == "access_level_denied"
    assert (data["level"], data["tool"], data["op"], data["scope"]) == (
        "read", "zoho_record", "create", "org")
    assert data["required_scopes"] == ["ZohoCRM.modules.CREATE"]
    assert data["retryable"] is False
    _appel("zoho_record", {"module": "Leads", "op": "get"})


def test_sans_op_l_op_par_defaut_est_jugee(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    _appel("zoho_record", {"module": "Leads"})               # défaut `list` : couvert

    async def _schema_create():
        return {"properties": {"op": {"default": "create", "enum": ["create"]}}}
    with pytest.raises(McpError):
        asyncio.run(gate.require_covered("zoho_note", {"module": "Leads",
                                                      "record_id": "1"}, _schema_create))


def test_un_outil_non_couvert_est_refuse(contexte):
    regler, _ = contexte
    regler(zohodesk={"group": "read"})
    with pytest.raises(McpError) as e:
        _appel("zohodesk_tickets", {})
    assert e.value.error.data["scope"] == "group"
    assert e.value.error.data["group_id"] == EQUIPE
    _appel("zohodesk_articles", {})


def test_sans_sub_rien_n_est_garde(contexte, monkeypatch):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: None)
    _appel("zoho_record", {"module": "Leads", "op": "delete"})


# --- visibilité et schéma servi -----------------------------------------------------

NOMS = {"zoho_record", "zoho_note", "zoho_modules", "zoho_connect", "zohodesk_tickets",
        "zohodesk_articles", "zohodesk_create_ticket", "serper_search", "oto_doc"}


def test_full_ne_masque_rien(contexte):
    assert gate.hidden_tools(NOMS, ORG, EQUIPE) == set()


def test_read_masque_ce_qu_il_ne_couvre_en_rien(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"}, zohodesk={"org": "read"})
    assert gate.hidden_tools(NOMS, ORG, EQUIPE) == {"zohodesk_tickets",
                                                    "zohodesk_create_ticket"}


def test_les_ops_servies_suivent_le_niveau(contexte):
    regler, _ = contexte
    assert gate.restricted_ops("sub-x") == {}
    regler(zoho={"org": "read"})
    assert gate.restricted_ops("sub-x") == {
        "zoho_record": {"list", "get", "search"}, "zoho_note": {"list"}}
    assert gate.restricted_ops(None) == {}


def test_le_schema_ne_garde_que_les_ops_couvertes():
    s = gate.restrict_schema(SCHEMA, frozenset({"list", "get"}))
    assert s["properties"]["op"]["enum"] == ["list", "get"]
    assert s["properties"]["op"]["default"] == "list"
    assert SCHEMA["properties"]["op"]["enum"][-1] == "delete", "l'original n'est pas touché"


def test_un_defaut_non_couvert_rend_op_obligatoire():
    s = gate.restrict_schema(SCHEMA, frozenset({"get"}))
    assert "default" not in s["properties"]["op"]
    assert "op" in s["required"] and "module" in s["required"]

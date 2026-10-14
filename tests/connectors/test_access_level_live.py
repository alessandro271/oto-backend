"""Niveau d'accès d'un connecteur, sur le chemin SERVI et en SQL réel.

Ce qu'on garde vert (`_build_mcp`, client MCP en mémoire, vrai PostgreSQL, sub tenu par
`OTO_MCP_DEV_SUB`) :

  1. le DDL : une base EXISTANTE dont le CHECK de `connector_settings` ignore `group`
     le reçoit au boot (`_poser_domaine`), et la pose d'une équipe s'écrit ;
  2. sous `read` posé par l'org : `zoho_record op=create` est refusé
     `access_level_denied`, en appel direct ET par `oto_call`, handler jamais atteint ;
     `op=list` atteint le handler par les deux chemins (sans ce témoin, un refus pour une
     autre raison passerait pour la garde) ;
  3. ce que voit l'agent : sous `read`, `zohodesk_tickets` n'est plus servi et l'enum
     `op` de `zoho_record` ne porte que les lectures ; au niveau `full`, rien ne bouge ;
  4. l'équipe restreint, n'élargit pas : `full` posé par une équipe sous une org `read`
     est refusé, et l'org garde le plafond ;
  5. l'app dédiée se range à part : posée, retrouvée par le consentement, jamais servie
     par la cascade d'appel.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.types import INVALID_PARAMS, ErrorData

from oto_mcp.mcp_errors import McpError

SUB = "sub-niveau-acces"
SONDE = "SONDE-NIVEAU : handler atteint"


@pytest.fixture(scope="module")
def orgs(live) -> dict:
    from oto_mcp import db, group_store, org_store
    db.upsert_user(SUB, email="niveau@example.test")
    org = org_store.create_org("Org niveau", created_by=SUB)
    org_store.add_org_member(org, SUB, "org_admin")
    assert org_store.set_active_org(SUB, org)
    equipe = group_store.create_group(org, "Equipe niveau")
    group_store.add_group_member(equipe, SUB)
    return {"org": org, "equipe": equipe}


@pytest.fixture
def etat(orgs, monkeypatch):
    """zoho et zohodesk ouverts et installés ; la résolution du credential remplacée par
    une sonde qui dit que le handler a été atteint (aucun appel amont)."""
    from oto_mcp import access, group_store
    from oto_mcp.connectors import access_levels, activation, selection

    monkeypatch.setenv("OTO_MCP_DEV_SUB", SUB)

    def _sonde(*_a, **_k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message=SONDE))
    monkeypatch.setattr(access, "resolve_credential", _sonde)
    monkeypatch.setattr(access, "resolve_credential_fields", _sonde)
    for c in ("zoho", "zohodesk"):
        activation.set_activation(c, True, None)
        selection.set_state(SUB, c, selection.ACTIVE, orgs["org"])
        access_levels.clear_level("org", orgs["org"], c)
        access_levels.clear_level("group", orgs["equipe"], c)
    group_store.clear_active_group(SUB)
    yield orgs
    for c in ("zoho", "zohodesk"):
        access_levels.clear_level("org", orgs["org"], c)
        access_levels.clear_level("group", orgs["equipe"], c)


def _client_run(fn):
    from fastmcp import Client

    from _mcp_app import static_mcp

    async def go():
        async with Client(static_mcp()) as c:
            return await fn(c)
    return asyncio.run(go())


def _appeler(chemin: str, outil: str, arguments: dict) -> str:
    from fastmcp.exceptions import ToolError

    async def fn(c):
        try:
            if chemin == "direct":
                r = await c.call_tool(outil, arguments)
            else:
                r = await c.call_tool("oto_call", {"name": outil, "arguments": arguments})
        except ToolError as e:
            return str(e)
        return r.content[0].text if r.content else str(r.structured_content)
    return _client_run(fn)


def _outils() -> dict:
    async def fn(c):
        return {t.name: t for t in await c.list_tools()}
    return _client_run(fn)


CHEMINS = ("direct", "oto_call")


# --- 1. le DDL, prouvé au boot sur une base qui existe déjà ------------------------

def test_le_boot_ouvre_le_check_a_group_sur_une_base_existante(orgs):
    from oto_mcp.db import _conn, init_db
    from oto_mcp.connectors import access_levels
    with _conn._connect() as conn:
        conn.execute("ALTER TABLE connector_settings DROP CONSTRAINT "
                     "connector_settings_scope_type_check")
        conn.execute("ALTER TABLE connector_settings ADD CONSTRAINT "
                     "connector_settings_scope_type_check "
                     "CHECK (scope_type IN ('platform', 'org'))")
    init_db()
    with _conn._connect() as conn:
        d = conn.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                         "WHERE conname = 'connector_settings_scope_type_check'"
                         ).fetchone()["d"]
    assert "'group'" in d
    access_levels.set_level("group", orgs["equipe"], "zoho", "read", SUB,
                            org_id=orgs["org"])
    assert access_levels.resolve("zoho", orgs["org"], orgs["equipe"]) == ("read", "group")
    assert access_levels.clear_level("group", orgs["equipe"], "zoho")


# --- 2. la garde d'appel, sur les deux chemins ---------------------------------------

@pytest.mark.parametrize("chemin", CHEMINS)
def test_sous_read_l_ecriture_est_refusee_la_lecture_servie(etat, chemin):
    from oto_mcp.connectors import access_levels
    access_levels.set_level("org", etat["org"], "zoho", "read", SUB)
    refus = _appeler(chemin, "zoho_record", {"module": "Leads", "op": "create",
                                             "data": {"Last_Name": "X"}})
    assert "access_level_denied" in refus and SONDE not in refus, refus
    assert "ZohoCRM.modules.CREATE" in refus
    lu = _appeler(chemin, "zoho_record", {"module": "Leads", "op": "list"})
    assert SONDE in lu, lu
    # Sans `op`, c'est l'op PAR DÉFAUT (`list`) qui est jugée : servie.
    assert SONDE in _appeler(chemin, "zoho_record", {"module": "Leads"})


@pytest.mark.parametrize("chemin", CHEMINS)
def test_au_niveau_full_rien_n_est_refuse(etat, chemin):
    texte = _appeler(chemin, "zoho_record", {"module": "Leads", "op": "delete",
                                             "record_id": "1"})
    assert SONDE in texte, texte


@pytest.mark.parametrize("chemin", CHEMINS)
def test_la_cle_d_une_equipe_restreinte_epinglee_hors_d_elle_est_refusee(etat, chemin):
    """L'équipe pose `read`, l'org reste `full`, l'appel se résout au niveau de l'org (pas
    d'équipe active) : `_instance=` épingle la clé de L'ÉQUIPE — c'est elle qui serait
    servie, c'est son niveau qui juge."""
    from oto_mcp.connectors import access_levels
    access_levels.set_level("group", etat["equipe"], "zoho", "read", SUB,
                            org_id=etat["org"])
    epingle = {"_instance": f"group:{etat['equipe']}:zoho"}
    refus = _appeler(chemin, "zoho_record", {"module": "Leads", "op": "delete",
                                             "record_id": "1", **epingle})
    assert "access_level_denied" in refus and SONDE not in refus, refus
    # Témoins : la lecture par la même clé est servie ; sans épingle, l'org `full` décide.
    assert SONDE in _appeler(chemin, "zoho_record", {"module": "Leads", "op": "get",
                                                     "record_id": "1", **epingle})
    assert SONDE in _appeler(chemin, "zoho_record", {"module": "Leads", "op": "delete",
                                                     "record_id": "1"})


# --- 3. ce que voit l'agent -------------------------------------------------------

def _enum_op(outil) -> list:
    return outil.inputSchema["properties"]["op"]["enum"]


def test_sous_read_les_outils_et_les_ops_non_couverts_disparaissent(etat):
    from oto_mcp.connectors import access_levels
    avant = _outils()
    assert "zohodesk_tickets" in avant
    assert set(_enum_op(avant["zoho_record"])) == {
        "list", "get", "search", "create", "update", "delete"}
    for c in ("zoho", "zohodesk"):
        access_levels.set_level("org", etat["org"], c, "read", SUB)
    apres = _outils()
    assert "zohodesk_tickets" not in apres and "zohodesk_create_ticket" not in apres
    assert "zohodesk_articles" in apres and "zoho_modules" in apres
    assert set(_enum_op(apres["zoho_record"])) == {"list", "get", "search"}
    assert _enum_op(apres["zoho_note"]) == ["list"]


# --- 4. l'équipe restreint, n'élargit pas -----------------------------------------

def test_l_equipe_ne_peut_pas_elargir_son_org(etat):
    from oto_mcp.connectors import access_levels
    access_levels.set_level("org", etat["org"], "zoho", "read", SUB)
    with pytest.raises(access_levels.AccessLevelError) as e:
        access_levels.set_level("group", etat["equipe"], "zoho", "full", SUB,
                                org_id=etat["org"])
    assert e.value.code == "access_level_wider_than_org"
    # L'org rouvre, l'équipe restreint : c'est l'équipe active qui décide.
    access_levels.clear_level("org", etat["org"], "zoho")
    access_levels.set_level("group", etat["equipe"], "zoho", "read", SUB,
                            org_id=etat["org"])
    assert access_levels.resolve("zoho", etat["org"], etat["equipe"]) == ("read", "group")
    assert access_levels.resolve("zoho", etat["org"], None) == ("full", "default")


# --- 5. l'app dédiée, rangée à part -----------------------------------------------

def test_l_app_dediee_sert_le_consentement_jamais_l_appel(orgs, monkeypatch):
    from oto_mcp import access, credentials_store as cs
    from oto_mcp.auth import zoho as zoho_oauth
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "7" * 64)
    app = {"client_id": "1000.EQUIPE", "client_secret": "s-equipe", "data_center": "com"}
    cs.set_dedicated_app("group", orgs["equipe"], "zoho", app, set_by=SUB)
    cs.set_dedicated_app("org", orgs["org"], "zoho",
                         {**app, "client_id": "1000.ORG"}, set_by=SUB)
    try:
        # L'équipe prime sur l'org ; sa région est celle de l'app.
        vue = zoho_oauth.app_fields("zoho", SUB, "", org=orgs["org"], group=orgs["equipe"])
        assert vue["client_id"] == "1000.EQUIPE"
        assert zoho_oauth.consent_region(vue, "") == "com"
        assert zoho_oauth.app_fields("zoho", SUB, "", org=orgs["org"],
                                     group=None)["client_id"] == "1000.ORG"
        assert set(cs.dedicated_app_owners("zoho", orgs["org"], orgs["equipe"])) == {
            "org", "group"}
        # La cascade d'appel ne la voit pas : aucune clé zoho pour ce sub.
        with pytest.raises(McpError):
            access.resolve_credential("zoho", want="auto", sub=SUB, emit_on_failure=False)
    finally:
        assert cs.clear_dedicated_app("group", orgs["equipe"], "zoho")
        assert cs.clear_dedicated_app("org", orgs["org"], "zoho")

"""`oto_procedure_app` — le graphe d'une procédure, peint par le bundle d'un front.

Trois choses à figer :
1. **sans `OTO_PROCEDURE_APP_BUNDLE`, rien ne s'enregistre** — ni outil, ni page ;
2. **la page charge le bundle déclaré**, et sa CSP admet son origine et les logos ;
3. **le tracé arrive à la vue SANS ses fences, le modèle ne lit qu'une ligne**, et le
   canal structuré survit à la chaîne servie (`UnSeulCanal`, signal #1083).
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from fastmcp import Client, FastMCP

BUNDLE = "https://app.example.com/mcp-app/process-graph.js"

TRACE = """┌──────────────────┐
│ 1. Trigger        │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 2. serper_search  │
└──────────────────┘"""

CORPS = f"# Veille\n\nLe déroulé.\n\n```\n{TRACE}\n```\n\n## Étapes\n\n1. Chercher.\n"


def _module(monkeypatch):
    monkeypatch.delitem(sys.modules, "oto_mcp.tools.procedure_app", raising=False)
    import oto_mcp.tools.procedure_app as pa
    return pa


def _stub_backend(monkeypatch, procedures: dict):
    from oto_mcp import access, org_store, providers
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub1")
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda otype, oid, slug, version=None: procedures.get((otype, oid, slug)))
    monkeypatch.setattr(providers, "public_catalog", lambda: [
        {"name": "serper", "label": "Serper", "category": "search",
         "logo_url": "https://img.logo.dev/serper.dev", "namespaces": ["serper"]},
        {"name": "hunter", "label": "Hunter.io", "category": "enrichment",
         "logo_url": "https://img.logo.dev/hunter.io", "namespaces": ["hunter"]},
    ])


def _banc(monkeypatch, pa):
    from _mcp_app import static_mcp

    from oto_mcp.middleware import un_seul_canal
    m = FastMCP("banc")
    for mw in static_mcp().middleware:
        m.add_middleware(mw)
    pa.register(m)
    un_seul_canal.retirer_les_schemas_vides(m)
    return m


def test_sans_bundle_declare_rien_ne_s_enregistre(monkeypatch):
    monkeypatch.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
    pa = _module(monkeypatch)
    m = FastMCP("banc")
    pa.register(m)

    async def lister():
        async with Client(m) as c:
            return await c.list_tools(), await c.list_resources()

    outils, pages = asyncio.run(lister())
    assert outils == [] and pages == []


def test_un_bundle_qui_n_est_pas_un_script_n_enregistre_rien(monkeypatch):
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", "https://app.example.com/mcp-app/")
    pa = _module(monkeypatch)
    m = FastMCP("banc")
    pa.register(m)

    async def lister():
        async with Client(m) as c:
            return await c.list_tools()

    assert asyncio.run(lister()) == []


def test_bout_en_bout_la_vue_recoit_le_trace_et_le_modele_une_ligne(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "veille"): {
        "slug": "veille", "title": "Veille", "version": 3, "body_md": CORPS}})

    async def appel():
        async with Client(m) as c:
            outil = next(t for t in await c.list_tools() if t.name == "oto_procedure_app")
            r = await c.call_tool("oto_procedure_app", {"slug": "veille"}, raise_on_error=False)
            uri = outil.meta["ui"]["resourceUri"]
            ui = (await c.read_resource(uri))[0]
            pages = await c.list_resources()
            return r, ui, next(p for p in pages if str(p.uri) == uri)

    r, ui, page = asyncio.run(appel())
    assert not r.is_error

    # La vue : le tracé nu, et les seuls connecteurs qu'il peut nommer.
    vue = r.structured_content
    assert vue is not None, "la vue n'a rien à peindre"
    assert vue["drawing"] == TRACE
    assert vue["title"] == "Veille" and vue["version"] == 3
    assert [c["name"] for c in vue["connectors"]] == ["serper"]

    # Le modèle : une ligne, qui l'envoie lire les étapes ailleurs — pas le tracé.
    texte = "".join(getattr(b, "text", "") for b in r.content)
    assert "oto_procedure op=get slug=veille" in texte
    assert "┌" not in texte

    # La page : le bundle déclaré et sa feuille, une CSP qui les admet.
    assert ui.mimeType == "text/html;profile=mcp-app"
    assert f'src="{BUNDLE}"' in ui.text
    assert 'href="https://app.example.com/mcp-app/process-graph.css"' in ui.text
    csp = json.dumps(page.meta)
    assert "https://app.example.com" in csp and "https://img.logo.dev" in csp
    assert page.meta["ui"]["prefersBorder"] is True


def test_l_equipe_active_complete_l_org(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    _stub_backend(monkeypatch, {("group", 7, "veille"): {
        "slug": "veille", "title": "Veille d'équipe", "version": 1, "body_md": CORPS}})
    from oto_mcp import access
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    row, scope = pa._lire("sub1", "veille")
    assert row["title"] == "Veille d'équipe" and scope == "group"


def test_sans_dessin_ou_sans_procedure_la_vue_le_dit(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "prose"): {
        "slug": "prose", "title": "Prose", "version": 1, "body_md": "# Prose\n\n1. Faire.\n"}})

    async def appel(slug):
        async with Client(m) as c:
            return await c.call_tool("oto_procedure_app", {"slug": slug}, raise_on_error=False)

    sans_dessin = asyncio.run(appel("prose"))
    assert sans_dessin.structured_content["drawing"] is None
    assert sans_dessin.structured_content["connectors"] == []
    # Sans dessin, le modèle est envoyé en dessiner un — jamais en Mermaid — puis rappeler.
    consigne = "".join(getattr(b, "text", "") for b in sans_dessin.content)
    assert "REQUIRED NEXT STEP" in consigne and "slug=procedure-flowchart" in consigne
    assert "oto_procedure op=set slug=prose scope=org expected_version=1" in consigne
    assert "Never Mermaid" in consigne and "oto_procedure_app slug=prose again" in consigne
    assert "ONLY THEN answer" in consigne

    absente = asyncio.run(appel("inconnue"))
    assert "inconnue" in absente.structured_content["message"]
    assert "oto_procedure op=list" in "".join(getattr(b, "text", "") for b in absente.content)


def test_oto_procedure_propose_le_graphe_la_ou_il_existe(monkeypatch):
    """La lecture d'une procédure par son slug renvoie l'agent la MONTRER ; une
    instance sans bundle ne propose pas un outil qu'elle n'a pas."""
    from oto_mcp import procedure_diagram, tool_alias
    from oto_mcp.capabilities import procedure_console as pc
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")

    async def lecture():
        return {"slug": "veille", "body_md": "…"}

    def lire(**kw):
        inp = pc.ProcedureInput(op="get", slug="veille", **kw)
        return asyncio.run(pc._avec_le_graphe(type("Ctx", (), {"sub": "s"})(), inp, lecture()))

    monkeypatch.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
    assert "show_graph" not in lire()

    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    assert "oto_procedure_app slug=veille" in lire()["show_graph"]
    # Une version archivée ou une procédure lue par son id : l'outil ne la relirait pas.
    assert "show_graph" not in lire(version=2)
    assert "show_graph" not in lire(guide_id=9)
    assert "oto_procedure_app slug=<slug>" in procedure_diagram.indice_du_graphe("s")


def test_un_bundle_qui_n_est_pas_un_script_est_refuse(monkeypatch):
    from oto_mcp import procedure_diagram
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", "ftp://x/process-graph.js")
    assert procedure_diagram.bundle_du_graphe() is None
    assert procedure_diagram.indice_du_graphe("s", "veille") is None

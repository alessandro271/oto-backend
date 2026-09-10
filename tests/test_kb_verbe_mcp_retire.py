"""Le verbe MCP `oto_kb` est RETIRÉ (10/09/2026) : il refuse en nommant le geste qui aboutit.

Décision d'Alexis : la « base de connaissance » n'existe plus comme concept — il n'y a que
projet, doc et procédure. La DONNÉE ne bouge pas : c'était déjà un projet ordinaire. La
route REST `POST /api/me/kb` RESTE, le tableau de bord la consomme (étape d'accueil, écran
Documents, sélecteur de documents) : seule la face MCP s'en va.

Pourquoi retirer plutôt que laisser dormir : la description disait « LEGACY, n'écris pas
ici », mais la RÉPONSE de `op=get` rendait le numéro du projet et le résumé « The org-wide
knowledge base: shared reference pages » — et dans 4 des 6 orgs où des agents ont écrit
dans une KB après la coupure du recrutement (08/09), l'agent venait d'appeler ce verbe. Le
verbe recrutait par sa réponse ; `oto_whoami` faisait de même avec son champ `knowledge`.

Trois chemins mènent au nom retiré, et chacun doit rendre LE MÊME refus :
1. `tools/call name=oto_kb` direct — fastmcp lève, l'enveloppe d'erreur classe ;
2. `oto_call(name="oto_kb")` — le chemin RÉEL : la notice prescrit `oto_call` pour un
   outil absent de la liste, et des procédures d'org (contenu client, qu'on ne corrige
   pas) nomment encore `oto_kb`. Avant ce lot il répondait « outil méta/spine —
   appelle-le directement », qui renvoie au chemin 1 : une boucle ;
3. `oto_tool_schema(name="oto_kb")` — ce qu'on lit avant d'appeler.
"""
from __future__ import annotations

import asyncio
import json

import fastmcp.server.context as _fc
import pytest
from mcp.types import INVALID_PARAMS

from oto_mcp import error_taxonomy, guide_store, instructions, openapi, outils_retires, providers
from oto_mcp.capabilities import registry
from oto_mcp.mcp_errors import McpError
from oto_mcp.middleware.error_envelope import ErrorEnvelopeMiddleware
from oto_mcp.tools import meta as _meta

from _mcp_app import static_mcp

RETIRE = "oto_kb"


def _dit_le_geste(message: str) -> None:
    """Ce que le refus DOIT porter, quel que soit le chemin qui y mène : le texte déclaré
    (aucun chemin ne se met à dire autre chose), et ce que ce texte doit contenir."""
    assert outils_retires.KB.message in message, message
    assert "retired" in message
    assert "ordinary PROJECT" in message, "une base de connaissance EST un projet"
    assert "`oto_project op=list`" in message, "le geste pour TROUVER le projet"
    assert "`oto_doc`" in message and "`project_id`" in message, "le geste pour ses pages"


def _catalogue_monte():
    return asyncio.run(static_mcp().list_tools(run_middleware=False))


def test_la_capacite_quitte_le_MCP_et_reste_servie_en_REST():
    cap = registry.by_key("me.kb")
    assert cap.mcp is None
    assert cap not in registry.caps_with_mcp()
    assert [(b.verb, b.path) for b in cap.rest_bindings()] == [("POST", "/api/me/kb")]
    assert "post" in openapi.build()["paths"]["/api/me/kb"], "le tableau de bord la consomme"


def test_le_catalogue_MONTE_ne_porte_plus_oto_kb():
    noms = {t.name for t in _catalogue_monte()}
    assert RETIRE not in noms
    # Contrôle positif : c'est bien le vrai catalogue (sinon « absent » ne prouve rien).
    assert {"oto_doc", "oto_project", "oto_search", "oto_call", "oto_tool_schema"} <= noms


class _Ctx:
    class _Msg:
        name = RETIRE
    message = _Msg()


def test_appel_direct__refuse_en_nommant_le_geste():
    """`tools/call` sur le serveur RÉELLEMENT monté, puis l'enveloppe d'erreur — celle
    qui répond à l'agent — sur l'exception que ce montage a réellement levée."""
    with pytest.raises(Exception) as leve:
        asyncio.run(static_mcp().call_tool(RETIRE, {"op": "get"}, run_middleware=False))
    info = error_taxonomy.classify(leve.value)
    _dit_le_geste(info.message)
    assert info.code == "unknown_tool"
    assert error_taxonomy.jsonrpc_code(info) == INVALID_PARAMS, "changer d'appel, pas réessayer"
    assert info.hint == outils_retires.KB.hint

    async def _suivant(_ctx):
        raise leve.value

    with pytest.raises(McpError) as servi:
        asyncio.run(ErrorEnvelopeMiddleware().on_call_tool(_Ctx(), _suivant))
    _dit_le_geste(servi.value.error.message)


async def _appelle(nom_du_tool: str, args: dict):
    """Le tool RÉELLEMENT monté, exécuté dans un contexte fastmcp (patron de
    `test_tool_alias_par_tenant`) — pas la closure recopiée."""
    tool = await static_mcp().get_tool(nom_du_tool)
    async with _fc.Context(fastmcp=static_mcp()):
        return await tool.run(args)


@pytest.mark.parametrize("nom_du_tool, args", [
    ("oto_call", {"name": RETIRE, "arguments": {"op": "get"}}),
    ("oto_tool_schema", {"name": RETIRE}),
])
def test_oto_call_et_oto_tool_schema_refusent_en_nommant_le_geste(nom_du_tool, args,
                                                                  monkeypatch):
    monkeypatch.setattr(_meta, "current_user_sub_from_token", lambda: "u-retrait")
    with pytest.raises(McpError) as ei:
        asyncio.run(_appelle(nom_du_tool, args))
    _dit_le_geste(ei.value.error.message)


def test_oto_whoami_ne_rend_plus_le_projet_de_l_ex_base(monkeypatch):
    """Le même recrutement, par la réponse d'un autre outil : « ta KB est le projet N »."""
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import whoami as whoami_tool

    monkeypatch.setattr(whoami_tool, "current_user_sub_from_token", lambda: "u-retrait")
    monkeypatch.setattr(access, "status_for", lambda sub: {"providers": {}})
    m = FastMCP("t")
    whoami_tool.register(m)
    tool = asyncio.run(m.get_tool("oto_whoami"))
    out = tool.fn(ctx=None)
    assert "account" in out and "connectors" in out, "contrôle positif : la vraie réponse"
    assert "knowledge" not in out
    assert "kb_project_id" not in json.dumps(out, default=str)
    assert "kb_project_id" not in (tool.description or "")


def test_oto_doc_dit_la_portee_des_liens_en_projets():
    """La résolution des `[[…]]` retombe bien sur le projet ancré de l'org
    (`db/backlinks._kb_project_of`) : le texte dit ce mécanisme, en projets."""
    desc = next(t for t in _catalogue_monte() if t.name == "oto_doc").description or ""
    assert "historical documents project" in desc
    assert "org KB" not in desc and "the KB" not in desc


def test_aucun_texte_servi_ne_nomme_plus_le_verbe_retire():
    """Un nom retiré qu'un texte servi continue de citer envoie l'agent droit sur le refus.
    Balaye description ET schéma : le bloc `Args:` d'une docstring part dans le schéma."""
    servis = {f"outil `{t.name}`": (t.description or "")
              + json.dumps(t.parameters or {}, ensure_ascii=False)
              for t in _catalogue_monte()}
    servis["carte des namespaces"] = providers.render_namespace_catalog()
    servis["socle de boot"] = instructions.render()
    servis["guide `notice` (fichier)"] = guide_store.file_guide("notice")["body_md"]
    assert "oto_doc" in servis["carte des namespaces"], "contrôle positif du balayage"
    citants = sorted(k for k, v in servis.items() if RETIRE in v)
    assert not citants, f"`{RETIRE}` encore cité par : {citants}"

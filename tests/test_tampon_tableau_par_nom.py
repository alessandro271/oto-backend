"""Le tampon « tableau désigné par son NOM » (`deprecations`, `OTO_MCP_COMPAT_TABLEAU_PAR_NOM`).

Le code métier refuse un nom de tableau (`datastore_by_name`, #1786) et ce refus reste
la règle : réglage ÉTEINT (le défaut), rien ne change. ALLUMÉ, le nom est traduit en
numéro AU BORD — face MCP (appel direct et relayé), face REST (chemin et corps des
capacités), portée d'un jeton — puis le code métier reçoit le numéro ; la désignation
laisse `ref_kind = name` dans le journal. Un nom qui ne désigne rien reste refusé comme
introuvable. La date de revue ne change rien au comportement servi. Sans base.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import types

import pytest

from oto_mcp import deprecations, interrupteurs, session_org
from oto_mcp.datastore import errors

ENV = "OTO_MCP_COMPAT_TABLEAU_PAR_NOM"


@pytest.fixture()
def resolution(monkeypatch):
    """La résolution d'avant le retrait, doublée : `vivier` désigne le tableau 204 dans
    la portée active, tout autre nom ne désigne rien. Rend les adresses résolues."""
    from oto_mcp import access, group_store, roles
    from oto_mcp.datastore import core as D

    vus: list = []
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org_id=None: [])
    monkeypatch.setattr(D.ownership, "vue_bornee", lambda: None)

    def resoudre(adresse, **k):
        vus.append(adresse)
        return {"id": 204, "datastore": "vivier"} if adresse in ("vivier", "204") else None

    monkeypatch.setattr(D.db, "resolve_datastore_ns", resoudre)
    return vus


@pytest.fixture()
def allume(monkeypatch):
    monkeypatch.setenv(ENV, "1")


@pytest.fixture()
def trace():
    """Le relevé de l'appel MCP et celui de la requête REST, comme le journal les pose."""
    mcp, rest = {}, {}
    t1, t2 = session_org.set_call_trace(mcp), session_org.set_ref_rest(rest)
    yield mcp, rest
    session_org.reset_ref_rest(t2)
    session_org.reset_call_trace(t1)


# ── Le réglage ───────────────────────────────────────────────────────────────

def test_eteint_par_defaut(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert deprecations.compat_tableau_par_nom_allume() is False
    deprecations.verifier_reglages()


def test_une_valeur_illisible_refuse_le_demarrage(monkeypatch):
    monkeypatch.setenv(ENV, "oui")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        deprecations.verifier_reglages()


def test_le_reglage_est_inventorie():
    from oto_mcp import env_inventory
    assert env_inventory.est_couverte(ENV)


# ── La traduction, une seule fonction ────────────────────────────────────────

def test_eteint_rien_ne_change(monkeypatch, resolution, trace):
    monkeypatch.delenv(ENV, raising=False)
    args = {"datastore": "vivier"}
    deprecations.traduire_arguments("data_write", args, "u-1")
    assert args == {"datastore": "vivier"} and resolution == []


def test_allume_un_nom_devient_son_numero_et_se_compte(allume, resolution, trace):
    args = {"datastore": "vivier", "row": {"a": 1}}
    deprecations.traduire_arguments("data_write", args, "u-1")
    assert args == {"datastore": "204", "row": {"a": 1}}
    assert trace[0]["ref_kind"] == "name" and trace[1]["ref_kind"] == "name"


def test_la_date_de_revue_passee_ne_change_rien(allume, resolution, trace, monkeypatch):
    """Une date de REVUE, jamais une date couperet : allumé, le tampon traduit après."""
    monkeypatch.setattr(deprecations, "REVUE_COMPAT_TABLEAU_PAR_NOM",
                        datetime.date(2000, 1, 1))
    args = {"datastore": "vivier"}
    deprecations.traduire_arguments("data_write", args, "u-1")
    assert args == {"datastore": "204"}


def test_un_numero_et_un_slot_ne_sont_jamais_touches(allume, resolution, trace):
    for adresse in ("204", "slot:vivier"):
        args = {"datastore": adresse}
        deprecations.traduire_arguments("data_write", args, "u-1")
        assert args == {"datastore": adresse}
    assert resolution == [] and "ref_kind" not in trace[0]


def test_un_nom_inconnu_reste_introuvable(allume, resolution, trace):
    with pytest.raises(deprecations.TableauIntrouvable) as e:
        deprecations.traduire_arguments("data_write", {"datastore": "inconnu"}, "u-1")
    assert str(e.value) == "datastore `inconnu` unknown"
    assert trace[0]["ref_kind"] == "name", "un nom refusé se compte aussi"


def test_la_creation_d_un_tableau_garde_son_nom(allume, resolution):
    for surface in ("data_create_datastore", "me.datastore.create_datastore"):
        args = {"datastore": "neuf"}
        deprecations.traduire_arguments(surface, args, "u-1")
        assert args == {"datastore": "neuf"}
    assert resolution == []


@pytest.mark.parametrize("surface,args,attendu", [
    ("oto_project", {"op": "link", "target_type": "tableau", "target_ref": "vivier"},
     {"op": "link", "target_type": "tableau", "target_ref": "204"}),
    ("oto_project", {"op": "link", "target_type": "procedure", "target_ref": "vivier"},
     {"op": "link", "target_type": "procedure", "target_ref": "vivier"}),
    ("oto_fleet", {"op": "create", "namespace": "vivier"},
     {"op": "create", "namespace": "204"}),
    ("oto_upload_url", {"datastore": "vivier"}, {"datastore": "204"}),
    ("oto_apps", {"op": "create", "manifest": {"tables": {"t": {"table": "vivier"}}}},
     {"op": "create", "manifest": {"tables": {"t": {"table": "204"}}}}),
    ("me.token.create",
     {"scopes": {"namespaces": {"vivier": "read"},
                 "issue": {"namespaces": {"vivier": "write", "7": "read"}}}},
     {"scopes": {"namespaces": {"204": "read"},
                 "issue": {"namespaces": {"204": "write", "7": "read"}}}}),
])
def test_les_capacites_que_le_retrait_a_fermees(allume, resolution, surface, args, attendu):
    deprecations.traduire_arguments(surface, args, "u-1")
    assert args == attendu


def test_le_code_metier_refuse_toujours_un_nom(allume, resolution):
    """La garde de #1786 tient : le tampon traduit AVANT, `_trouver` ne connaît que le
    numéro, même allumé."""
    from oto_mcp.datastore import core as D
    with pytest.raises(errors.TableauParNom):
        D.make_store("u-1")._resolve("vivier")
    assert resolution == []


# ── Face MCP : le middleware ─────────────────────────────────────────────────

def _appel(name: str, args: dict):
    from oto_mcp.middleware.compat import CompatMiddleware

    vu: dict = {}

    async def suite(context):
        vu.update(context.message.arguments)
        return "servi"

    ctx = types.SimpleNamespace(message=types.SimpleNamespace(name=name, arguments=args))
    return asyncio.run(CompatMiddleware().on_call_tool(ctx, suite)), vu


@pytest.fixture()
def sub(monkeypatch):
    from oto_mcp.middleware import compat
    monkeypatch.setattr(compat, "current_user_sub_from_token", lambda: "u-1")


def test_mcp_allume_l_outil_recoit_le_numero(allume, resolution, trace, sub):
    servi, vu = _appel("data_write", {"datastore": "vivier"})
    assert servi == "servi" and vu == {"datastore": "204"}
    assert trace[0]["ref_kind"] == "name"


def test_mcp_eteint_l_outil_recoit_le_nom_et_le_refuse(monkeypatch, resolution, sub):
    monkeypatch.delenv(ENV, raising=False)
    servi, vu = _appel("data_write", {"datastore": "vivier"})
    assert vu == {"datastore": "vivier"} and resolution == []


def test_mcp_un_nom_inconnu_est_introuvable(allume, resolution, trace, sub):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as e:
        _appel("data_write", {"datastore": "inconnu"})
    assert "datastore `inconnu` unknown" in str(e.value)
    assert e.value.error.data["oto"]["code"] == "datastore_not_found"


# ── Face REST : l'adaptateur des capacités ───────────────────────────────────

def _route(adresse: str):
    from pydantic import BaseModel
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities._types import Capability, ResolvedCtx, RestBinding

    appels: list = []

    class Entree(BaseModel):
        datastore: str

    class Sortie(BaseModel):
        ok: bool

    cap = Capability(
        key="t.tableau", handler=lambda ctx, inp: appels.append(inp.datastore) or {"ok": True},
        Input=Entree, Output=Sortie,
        authz=lambda raw, inp: ResolvedCtx(sub=raw.sub, org_id=None),
        rest=RestBinding(verb="GET", path="/api/datastores/{datastore}/t"))

    async def authenticate(request, verifier, **kw):
        return "u-1", None

    async def options(request):
        return JSONResponse({})

    routes = _rest_adapter.make_routes(
        None, authenticate,
        lambda request, data, status=200: JSONResponse(data, status),
        lambda request, status, code, detail=None, **kw: JSONResponse(
            {"error": code, "detail": detail}, status),
        options, [cap])
    return TestClient(Starlette(routes=routes)).get(f"/api/datastores/{adresse}/t"), appels


def test_rest_allume_la_route_recoit_le_numero(allume, resolution):
    r, appels = _route("vivier")
    assert r.status_code == 200, r.text
    assert appels == ["204"]


def test_rest_eteint_le_nom_reste_refuse(monkeypatch, resolution):
    monkeypatch.delenv(ENV, raising=False)
    r, appels = _route("vivier")
    assert r.status_code == 400 and r.json()["error"] == "datastore_by_name"
    assert appels == []


def test_rest_un_numero_marche_toujours(allume, resolution):
    r, appels = _route("204")
    assert r.status_code == 200 and appels == ["204"]


def test_rest_un_nom_inconnu_est_introuvable(allume, resolution):
    r, appels = _route("inconnu")
    assert r.status_code == 404 and r.json()["error"] == "datastore_not_found"
    assert appels == []


# ── Face REST : un jeton porté juge le NUMÉRO que le nom désigne ─────────────

def _req(method: str, path: str):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "http_version": "1.1", "headers": [(b"authorization", b"Bearer oto_deadbeef")],
    })


@pytest.fixture()
def jeton(monkeypatch):
    from oto_mcp.api import routes as api_routes
    from oto_mcp.auth import token_scopes

    row = {"sub": "u-1", "scopes": {"namespaces": {"204": "read"}}}
    monkeypatch.setattr(api_routes.db, "verify_api_token", lambda t: row)
    monkeypatch.setattr(api_routes.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(api_routes.db, "get_suspension", lambda sub: None)
    yield
    token_scopes.set_current(None)


def _auth(path: str):
    from oto_mcp.api import routes as api_routes

    class Verifier:
        async def verify_token(self, token):
            return None

    sub, err = asyncio.run(api_routes._authenticate(_req("GET", path), verifier=Verifier()))
    return sub, (json.loads(bytes(err.body).decode()) if err else None)


def test_jeton_allume_le_nom_est_juge_sur_son_numero(allume, resolution, jeton):
    assert _auth("/api/datastores/vivier/rows") == ("u-1", None)


def test_jeton_eteint_le_nom_est_refuse(monkeypatch, resolution, jeton):
    monkeypatch.delenv(ENV, raising=False)
    sub, err = _auth("/api/datastores/vivier/rows")
    assert sub is None and err["error"] == "datastore_by_name"


def test_jeton_allume_un_nom_inconnu_est_introuvable(allume, resolution, jeton):
    sub, err = _auth("/api/datastores/inconnu/rows")
    assert sub is None and err["error"] == "datastore_not_found"

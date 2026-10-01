"""La toolbox du membre : une capacité par geste, servie à l'agent ET au dashboard (#429).

`oto_disable_tool` / `oto_enable_tool` étaient écrits à la main dans `tools/meta.py`,
miroir de `POST|DELETE /api/me/tools/{name}` : deux implémentations du même geste, deux
autz à tenir en phase. Ils sont désormais la face MCP des capacités `me.tools.disable`
/ `me.tools.enable`. Ces bancs exécutent l'outil RÉELLEMENT MONTÉ (`_test_mcp()`) et
tiennent ce que l'agent recevait déjà :

1. le nom sous la forme du tenant se canonicalise pour la denylist, et revient sous la
   forme MONTRÉE ;
2. la session courante perd l'outil tout de suite (`disable_components`) ;
3. un nom inconnu et un outil protégé sont refusés par leur nom, rien n'est écrit ;
4. démasquer un outil masqué par défaut pose l'override positif.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import fastmcp.server.context as _fc
import pytest

from mcp.shared.exceptions import McpError

from oto_mcp import tenancy
from oto_mcp.capabilities import _mcp_adapter
from oto_mcp.capabilities import tools_me as tm

_SUB = "u-1"
_SUB_TENANT = "acme:u-1"


@pytest.fixture()
def compte(monkeypatch):
    """Un compte sans org (aucun écho d'org), un coffre de préférences qui enregistre,
    et les deux gestes de session tracés au lieu d'être joués."""
    ecrits: list = []
    monkeypatch.setattr(tm.access, "current_org", lambda sub: None)
    monkeypatch.setattr(tm.access, "get_user_role", lambda sub: "member")
    for nom in ("add_user_disabled_tool", "remove_user_disabled_tool",
                "add_user_enabled_tool", "remove_user_enabled_tool"):
        monkeypatch.setattr(tm.db, nom,
                            (lambda n: lambda *a: ecrits.append((n,) + a))(nom))

    def _trace(geste):
        async def _f(ctx, names, components):
            ecrits.append((geste, tuple(sorted(names))))
        return _f

    monkeypatch.setattr(tm, "disable_components", _trace("session_masque"))
    monkeypatch.setattr(tm, "enable_components", _trace("session_demasque"))
    return ecrits


def _comme(monkeypatch, sub):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: sub)


async def _appelle(nom: str, args: dict) -> dict:
    tool = await _test_mcp().get_tool(nom)
    async with _fc.Context(fastmcp=_test_mcp()):
        return (await tool.run(args)).structured_content


@pytest.mark.asyncio
async def test_masquer_ecrit_la_denylist_et_retire_l_outil_de_la_session(monkeypatch, compte):
    ecrits = compte
    _comme(monkeypatch, _SUB)
    out = await _appelle("oto_disable_tool", {"name": "fr_get"})
    assert out == {"ok": True, "name": "fr_get", "enabled": False}
    assert ecrits == [("add_user_disabled_tool", _SUB, "fr_get", 0),
                      ("remove_user_enabled_tool", _SUB, "fr_get", 0),
                      ("session_masque", ("fr_get",))]


@pytest.mark.asyncio
async def test_un_nom_inconnu_est_refuse_par_son_nom(monkeypatch, compte):
    ecrits = compte
    _comme(monkeypatch, _SUB)
    with pytest.raises(McpError, match="Unknown tool `pas_un_outil`"):
        await _appelle("oto_disable_tool", {"name": "pas_un_outil"})
    assert ecrits == [], "rien ne doit être écrit quand la bascule est refusée"


@pytest.mark.asyncio
async def test_un_outil_protege_est_refuse(monkeypatch, compte):
    ecrits = compte
    _comme(monkeypatch, _SUB)
    with pytest.raises(McpError, match="is protected"):
        await _appelle("oto_disable_tool", {"name": "oto_whoami"})
    assert ecrits == []


@pytest.mark.asyncio
async def test_demasquer_un_masque_par_defaut_pose_l_override(monkeypatch, compte):
    ecrits = compte
    _comme(monkeypatch, _SUB)
    out = await _appelle("oto_enable_tool", {"name": "browser_eval"})
    assert out == {"ok": True, "name": "browser_eval", "enabled": True}
    assert ecrits == [("remove_user_disabled_tool", _SUB, "browser_eval", 0),
                      ("add_user_enabled_tool", _SUB, "browser_eval", 0),
                      ("session_demasque", ("browser_eval",))]


@pytest.fixture
def tenant_acme():
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "tool_prefix": "acme",
                  "issuer": "https://auth.acme.test/oidc"}])))
    yield
    tenancy.install(avant)


@pytest.mark.asyncio
async def test_le_nom_du_tenant_s_ecrit_canonique_et_revient_montre(
        monkeypatch, compte, tenant_acme):
    """L'agent d'un tenant lit `acme_trigger` dans sa liste et le recopie : la denylist
    s'écrit sous le nom canonique `oto_trigger`, la réponse rend la forme qu'il a lue."""
    ecrits = compte
    _comme(monkeypatch, _SUB_TENANT)
    out = await _appelle("oto_disable_tool", {"name": "acme_trigger"})
    assert out["name"] == "acme_trigger"
    assert ecrits[0] == ("add_user_disabled_tool", _SUB_TENANT, "oto_trigger", 0)

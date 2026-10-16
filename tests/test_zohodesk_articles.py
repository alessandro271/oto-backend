"""Zoho Desk — articles du Help Center : recherche par mot-clé et vue légère.

Signal d'usage du 15/10/2026 : sans recherche, trouver un article demandait de paginer
plus de 300 articles, chacun rendu avec son propriétaire, ses traductions et ses
compteurs. La recherche existe chez Zoho (`/articles/search`) ; le défaut d'une liste
ou d'une recherche est une vue légère, et le corps complet reste à `zohodesk_article`.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

ARTICLE = {
    "id": "7", "title": "Reset a password", "status": "Published",
    "summary": "How to reset…", "modifiedTime": "2026-10-01T10:00:00.000Z",
    "webUrl": "https://desk.example.test/a/7", "portalUrl": "https://help.example.test/7",
    "category": {"name": "Account", "id": "3"},
    "owner": {"name": "Agent"}, "availableLocaleTranslations": [{"locale": "en"}],
    "viewCount": "12",
}
LEGER = {"id": "7", "title": "Reset a password", "status": "Published",
         "summary": "How to reset…", "modifiedTime": "2026-10-01T10:00:00.000Z",
         "webUrl": "https://desk.example.test/a/7",
         "portalUrl": "https://help.example.test/7", "category": "Account"}


@pytest.fixture
def client(monkeypatch):
    from oto.tools.zohodesk import client as desk_client_mod
    from oto_mcp import access

    inst = MagicMock()
    monkeypatch.setattr(desk_client_mod, "ZohoDeskClient", lambda **kw: inst)
    monkeypatch.setattr(access, "resolve_credential_fields", lambda *a, **k: {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "data_center": "eu"})
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import zohodesk as D

    m = FastMCP("t")
    D.register(m)
    return asyncio.run(m.get_tool(name)).fn


def test_la_recherche_passe_les_criteres_et_rend_la_vue_legere(client):
    client.search_articles.return_value = {"data": [ARTICLE], "count": 31}
    out = _tool("zohodesk_search_articles")(query="password", limit=5)
    client.search_articles.assert_called_once_with(
        query="password", title=None, tag=None, category_id=None, department_id=None,
        from_index=0, limit=5, sort_by=None)
    assert out == {"data": [LEGER], "count": 31}, "l'enveloppe (count) reste"


def test_full_rend_le_brut(client):
    client.search_articles.return_value = {"data": [ARTICLE], "count": 1}
    assert _tool("zohodesk_search_articles")(tag="account", full=True) == {
        "data": [ARTICLE], "count": 1}


def test_une_recherche_sans_critere_est_refusee(client):
    with pytest.raises(McpError, match="at least one search criterion"):
        _tool("zohodesk_search_articles")()
    client.search_articles.assert_not_called()


def test_la_liste_rend_aussi_la_vue_legere(client):
    client.list_articles.return_value = {"data": [ARTICLE]}
    assert _tool("zohodesk_articles")() == {"data": [LEGER]}


def test_la_recherche_a_son_scope():
    from oto_mcp.providers.zohodesk import ACCESS_LEVELS

    assert ACCESS_LEVELS["tools"]["zohodesk_search_articles"] == (
        "Desk.search.READ", "Desk.articles.READ")

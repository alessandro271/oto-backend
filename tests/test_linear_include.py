"""`include` sur `linear_issue op=get` et `linear_project op=get`.

Un run qui annonce ce qui vient d'être livré doit dire QUI l'avait demandé et
QUAND le ticket a atteint un statut. Linear porte les trois lectures sur `Issue`
(`needs`, `relations`/`inverseRelations`, `history`) et les besoins clients sur
`Project` ; le jeu de champs par défaut du client ne les demandait pas.

Verrouille : la requête porte les champs demandés et eux seuls, l'historique ne
garde que les changements de statut, du plus ancien au plus récent, en suivant
ses pages, `include` est refusé hors `op=get`, et `get` sans `include` reste
l'appel d'avant (`test_linear.py::test_issue_get_requires_id`).

Les requêtes ont été validées contre le SDL publié de Linear (`schema.graphql`
de `@linear/sdk`, lu le 22/10/2026) ; ici, seule leur construction est jouée.
"""
import asyncio

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import linear


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


@pytest.fixture()
def tool():
    """Le module enregistré avec `LinearClient` mocké ; le mock garde les VRAIS
    jeux de champs par défaut, que `include` complète."""
    from unittest.mock import patch
    from fastmcp import FastMCP
    from oto.tools.linear.client import LinearClient as Real

    patcher = patch("oto.tools.linear.client.LinearClient")
    cls = patcher.start()
    cls._ISSUE_FIELDS = Real._ISSUE_FIELDS
    cls._PROJECT_FIELDS = Real._PROJECT_FIELDS
    m = FastMCP("t")
    linear.register(m)
    try:
        yield (lambda name: asyncio.run(m.get_tool(name)).fn), cls.return_value
    finally:
        patcher.stop()


def _fields(call):
    return call.kwargs["fields"]


def test_client_keeps_the_default_field_sets():
    """`include` complète `_ISSUE_FIELDS` / `_PROJECT_FIELDS` d'oto-core : un
    renommage là-bas casserait `op=get` au bump du pin, ici d'abord."""
    from oto.tools.linear.client import LinearClient
    assert "identifier" in LinearClient._ISSUE_FIELDS
    assert "status" in LinearClient._PROJECT_FIELDS


def test_needs_and_relations_travel_in_one_query(tool):
    fn, c = tool
    c.get_issue.return_value = {"id": "i1", "needs": {"nodes": []}}
    fn("linear_issue")(op="get", issue_id="i1", include=["needs", "relations"])

    c.get_issue.assert_called_once()
    fields = _fields(c.get_issue.call_args)
    assert "identifier" in fields                      # the default set stays
    assert "needs(first: 50)" in fields and "customer { id name domains }" in fields
    assert "relations(first: 50)" in fields and "inverseRelations(first: 50)" in fields
    assert "history" not in fields


def test_history_keeps_status_changes_oldest_first_across_pages(tool):
    fn, c = tool
    move = lambda at, frm, to: {"createdAt": at, "fromState": {"name": frm}, "toState": {"name": to}}
    edit = {"createdAt": "2026-10-20T09:00:00Z", "fromState": None, "toState": None}
    c.get_issue.side_effect = [
        {"id": "i1", "history": {
            "nodes": [move("2026-10-21T10:00:00Z", "In Review", "On prod"), edit],
            "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"}}},
        {"history": {
            "nodes": [move("2026-10-01T08:00:00Z", "Todo", "In Review")],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    out = fn("linear_issue")(op="get", issue_id="i1", include=["history"])

    assert "history" not in out
    assert [n["toState"]["name"] for n in out["state_history"]["nodes"]] == ["In Review", "On prod"]
    assert out["state_history"]["truncated"] is False
    second = _fields(c.get_issue.call_args_list[1])
    assert 'after: "cur-1"' in second and "identifier" not in second


def test_history_says_when_it_stopped_short(tool, monkeypatch):
    fn, c = tool
    monkeypatch.setattr(linear, "_HISTORY_MAX_PAGES", 2)
    page = {"history": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}
    c.get_issue.side_effect = [dict(page, id="i1"), dict(page), dict(page)]
    out = fn("linear_issue")(op="get", issue_id="i1", include=["history"])
    assert c.get_issue.call_count == 2
    assert out["state_history"]["truncated"] is True


def test_include_is_refused_outside_get(tool):
    fn, c = tool
    with pytest.raises(McpError, match="op='list' does not use include"):
        fn("linear_issue")(op="list", include=["needs"])
    with pytest.raises(McpError, match="op='list' does not use include"):
        fn("linear_project")(op="list", include=["needs"])
    c.list_issues.assert_not_called()
    c.list_projects.assert_not_called()


def test_project_needs_name_the_issue_they_were_attached_to(tool):
    fn, c = tool
    c.get_project.return_value = {"id": "p1"}
    fn("linear_project")(op="get", project_id="p1", include=["needs"])
    fields = _fields(c.get_project.call_args)
    assert "status" in fields and "needs(first: 50)" in fields
    assert "issue { id identifier title url }" in fields


def test_project_get_without_include_is_unchanged(tool):
    fn, c = tool
    c.get_project.return_value = {"id": "p1"}
    fn("linear_project")(op="get", project_id="p1")
    c.get_project.assert_called_once_with("p1")

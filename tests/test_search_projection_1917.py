"""Search pages light enough to read (signals #1908, #1917, 20-21 Nov 2026).

`leexi_calls op=search` served whole calls (prompt outputs, chapters, tasks: 30-40k
characters each), and `linear_issue op=list/search` every issue with its full
description (100-120k characters for one page): together they filled a run's context
before its first step ended. Leexi's search now defaults to the metadata view its
docstring always promised, through `output_projection.summarize` and a FIXED list of
body columns; Linear's list/search take `fields`.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from oto_mcp.mcp_errors import McpError

_LOURD = "x" * 5000


def _call_leexi(i: int, *, lourd: bool = True) -> dict:
    c = {"uuid": f"c{i}", "title": f"Call {i}", "performed_at": "2026-01-01T10:00:00Z",
         "owner": {"uuid": "u1", "name": "Owner"}, "duration": 1800,
         "summary": "Short summary."}
    if lourd:
        c |= {"prompts": [{"name": "Summary", "text": _LOURD}],
              "chapters": [{"title": "Intro", "summary": _LOURD}],
              "tasks": [{"text": "a task"}, {"text": "another"}]}
    return c


# --- Leexi ----------------------------------------------------------------------

def test_leexi_search_view_serves_body_columns_as_their_size():
    from oto_mcp.tools.leexi import _slim_calls

    page = {"success": True, "data": [_call_leexi(1), _call_leexi(2)],
            "pagination": {"page": 1, "items": 2, "count": 9}}
    out = _slim_calls(page, None)
    row = out["data"][0]
    for k in ("prompts", "chapters", "tasks"):
        assert k not in row
    assert row["prompts_length"] == 1 and row["tasks_length"] == 2
    assert row["summary"] == "Short summary." and row["owner"]["uuid"] == "u1"
    assert out["pagination"] == page["pagination"], "the envelope stays"
    assert out["projection"]["omitted"] == ["chapters", "prompts", "tasks"]
    assert page["data"][0]["prompts"], "the upstream payload is not mutated"


def test_leexi_columns_are_the_same_on_every_page():
    """A fixed list, not a weight threshold: a light page has the same keys as a
    heavy one (Alexis's review, #1890)."""
    from oto_mcp.tools.leexi import _slim_calls

    lourde = _slim_calls({"data": [_call_leexi(1)]}, None)["data"][0]
    legere = _slim_calls({"data": [_call_leexi(2, lourd=False)]}, None)["data"][0]
    assert set(lourde) == set(legere)
    assert legere["chapters_length"] == 0


def test_leexi_star_returns_the_raw_page():
    from oto_mcp.tools.leexi import _slim_calls

    page = {"data": [_call_leexi(1)]}
    assert _slim_calls(page, ["*"]) is page


def test_leexi_fields_keeps_the_named_keys_and_the_uuid():
    from oto_mcp.tools.leexi import _slim_calls

    out = _slim_calls({"data": [_call_leexi(1)], "pagination": {"page": 1}},
                     ["title", "chapters"])
    row = out["data"][0]
    assert row["title"] == "Call 1" and row["uuid"] == "c1"
    assert row["chapters"][0]["title"] == "Intro", "a body named in fields is served"
    assert "owner" not in row
    assert out["pagination"] == {"page": 1}


@pytest.fixture
def leexi_tool(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda *a, **k: {"key_id": "i", "key_secret": "s"})
    with patch("oto.tools.leexi.client.LeexiClient") as cls:
        from oto_mcp.tools import leexi
        m = FastMCP("t")
        leexi.register(m)
        yield asyncio.run(m.get_tool("leexi_calls")).fn, cls.return_value


def test_leexi_search_tool_serves_the_light_view_by_default(leexi_tool):
    fn, cli = leexi_tool
    cli.list_calls.return_value = {"data": [_call_leexi(1)]}
    assert "prompts" not in fn(op="search", items=100)["data"][0]
    assert fn(op="search", fields=["*"])["data"][0]["prompts"]


def test_leexi_with_transcript_is_served_whole(leexi_tool):
    """Asking for the transcript and getting it as a size would be a lie."""
    fn, cli = leexi_tool
    cli.list_calls.return_value = {"data": [{**_call_leexi(1), "simple_transcript": _LOURD}]}
    assert fn(op="search", with_transcript=True)["data"][0]["simple_transcript"] == _LOURD


@pytest.mark.parametrize("op,kw,match", [
    ("search", {"fields": []}, "non-empty list"),
    ("search", {"fields": {"a": 1}}, "non-empty list"),
    ("create", {"fields": ["title"]}, "an object"),
    ("get", {"call_uuid": "c1", "fields": ["title"]}, "does not use"),
])
def test_leexi_fields_shape_is_checked_per_op(leexi_tool, op, kw, match):
    fn, cli = leexi_tool
    with pytest.raises(McpError, match=match):
        fn(op=op, **kw)
    cli.get_call.assert_not_called()
    cli.create_call.assert_not_called()


# --- Linear ---------------------------------------------------------------------

@pytest.fixture
def linear_issue(monkeypatch):
    from oto_mcp.tools import linear

    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))
    patcher = patch("oto.tools.linear.client.LinearClient")
    cls = patcher.start()
    m = FastMCP("t")
    linear.register(m)
    yield asyncio.run(m.get_tool("linear_issue")).fn, cls.return_value
    patcher.stop()


def _issues():
    return {"nodes": [{"id": "i1", "identifier": "ENG-1", "title": "A",
                       "description": _LOURD, "state": {"name": "Todo"}},
                      {"id": "i2", "identifier": "ENG-2", "title": "B",
                       "description": _LOURD, "state": {"name": "Done"}}],
            "pageInfo": {"hasNextPage": True, "endCursor": "E"}}


@pytest.mark.parametrize("op,kw", [("search", {"query": "example"}), ("list", {})])
def test_linear_fields_project_list_and_search(linear_issue, op, kw):
    fn, cli = linear_issue
    cli.search_issues.return_value = _issues()
    cli.list_issues.return_value = _issues()
    out = fn(op=op, fields=["title", "state"], **kw)
    assert out["nodes"][0] == {"id": "i1", "identifier": "ENG-1", "title": "A",
                               "state": {"name": "Todo"}}
    assert out["pageInfo"] == {"hasNextPage": True, "endCursor": "E"}
    assert "description" in out["selection"]["omitted"]
    assert "missing" not in out["selection"]


def test_linear_without_fields_is_unchanged(linear_issue):
    fn, cli = linear_issue
    cli.search_issues.return_value = _issues()
    assert fn(op="search", query="example") == _issues()


def test_linear_fields_names_an_absent_key(linear_issue):
    fn, cli = linear_issue
    cli.list_issues.return_value = _issues()
    out = fn(op="list", fields=["title", "estimate"])
    assert out["selection"]["missing"] == ["estimate"]


def test_linear_empty_fields_is_refused(linear_issue):
    fn, cli = linear_issue
    with pytest.raises(McpError, match="empty list"):
        fn(op="list", fields=[])
    cli.list_issues.assert_not_called()


def test_linear_fields_is_refused_outside_list_and_search(linear_issue):
    fn, cli = linear_issue
    with pytest.raises(McpError, match="does not use fields"):
        fn(op="get", issue_id="i1", fields=["title"])
    cli.get_issue.assert_not_called()


# --- the shared notice seam ---------------------------------------------------------

def test_with_notice_never_overwrites_a_field_of_the_response():
    from oto_mcp.output_projection import with_notice

    assert with_notice({"a": 1}, {"n": 1}) == {"a": 1, "selection": {"n": 1}}
    assert with_notice({"selection": "theirs"}, {"n": 1}) == {
        "selection": "theirs", "oto_selection": {"n": 1}}
    assert with_notice([1], {"n": 1}) == {"result": [1], "selection": {"n": 1}}
    assert with_notice({"a": 1}, None) == {"a": 1}

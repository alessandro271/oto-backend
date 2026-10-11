"""DataForSEO connector — SEO data (keywords, domains, backlinks), pay-as-you-go.

Locks: the registry entry (two-field credential, byo + CLOSED paid platform
key), the MCP surface, the op dispatch to the oto-core client, the refusals
made BEFORE a paid call (missing location/language, unused argument, limit),
the `login:password` platform key split, the billing trace (real USD cost →
micro-dollars), the shared key's balance kept private, and the trimmed view.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import dataforseo as dfs_tool

EXPECTED_TOOLS = {"dataforseo_keywords", "dataforseo_domain", "dataforseo_backlinks",
                  "dataforseo_account"}


def _task(result=None, cost=0.0103):
    return {"id": "t", "status_code": 20000, "cost": cost,
            "result": result if result is not None else [{"items": []}]}


@pytest.fixture()
def env():
    """A registered surface over a mocked client and a mocked credential."""
    from fastmcp import FastMCP

    state = SimpleNamespace(
        rc=SimpleNamespace(fields={"login": "me@example.com", "password": "pw"},
                           config={}, is_platform=False),
        traces=[], client=MagicMock())
    for m in ("keyword_overview", "google_ads_search_volume", "bulk_keyword_difficulty",
              "search_intent", "keyword_ideas", "keyword_suggestions", "related_keywords",
              "domain_rank_overview", "ranked_keywords", "relevant_pages",
              "competitors_domain", "keywords_for_site", "domain_intersection",
              "bulk_traffic_estimation", "backlinks_summary", "backlinks_list",
              "referring_domains", "anchors", "bulk_ranks"):
        getattr(state.client, m).return_value = _task()
    with patch("oto.tools.dataforseo.DataForSeoClient") as cls, \
            patch.object(dfs_tool.access, "resolve_credential",
                         side_effect=lambda *a, **k: state.rc), \
            patch.object(dfs_tool.session_org, "note_call_trace",
                         side_effect=lambda **v: state.traces.append(v)):
        cls.side_effect = lambda login, password: (
            state.__setattr__("creds", (login, password)) or state.client)
        m = FastMCP("t")
        dfs_tool.register(m)
        state.call = lambda name, **kw: asyncio.run(m.get_tool(name)).fn(**kw)
        yield state


# --- registry -----------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["dataforseo"]
    assert c.kind == "tools"
    assert c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # Paid key: posted closed, served to granted orgs only (`set_platform_key`).
    assert c.platform_key_open is False
    assert [(f.name, f.secret) for f in c.secret_fields] == [("login", False),
                                                            ("password", True)]
    assert c.category == "Marketing"
    assert c.publisher_name == "DataForSEO"


def test_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["dataforseo"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


def test_tools_register_under_namespace(env):
    from fastmcp import FastMCP
    m = FastMCP("t")
    dfs_tool.register(m)
    tools = {t.name: t for t in asyncio.run(m._list_tools())}
    assert set(tools) == EXPECTED_TOOLS
    assert all(namespace_of(n) == "dataforseo" for n in tools)
    assert all(t.description for t in tools.values())


# --- credential -----------------------------------------------------------------

def test_two_fields(env):
    env.call("dataforseo_backlinks", op="summary", target="example.com")
    assert env.creds == ("me@example.com", "pw")


@pytest.mark.parametrize("fields", [
    {"password": "me@example.com:pw"},          # raw platform value → first secret field
    {"login": "me@example.com:pw"},
    {"key": "me@example.com:pw"},
])
def test_platform_value_login_colon_password(env, fields):
    env.rc = SimpleNamespace(fields=fields, config={}, is_platform=True)
    env.call("dataforseo_backlinks", op="summary", target="example.com")
    assert env.creds == ("me@example.com", "pw")


def test_password_may_contain_a_colon(env):
    env.rc = SimpleNamespace(fields={"password": "me@example.com:p:w"}, config={},
                             is_platform=True)
    env.call("dataforseo_backlinks", op="summary", target="example.com")
    assert env.creds == ("me@example.com", "p:w")


def test_incomplete_credential_is_named(env):
    env.rc = SimpleNamespace(fields={"password": "pw"}, config={}, is_platform=False)
    with pytest.raises(McpError, match="login"):
        env.call("dataforseo_backlinks", op="summary", target="example.com")


# --- dispatch -------------------------------------------------------------------

def test_keywords_overview(env):
    env.call("dataforseo_keywords", op="overview", keywords=["crm"], location=2250,
             language="fr")
    env.client.keyword_overview.assert_called_once_with(
        ["crm"], options=None, location_code=2250, language_code="fr")


def test_location_name_and_language_name(env):
    env.call("dataforseo_keywords", op="ideas", keywords=["crm"], location="France",
             language="French", limit=5)
    kw = env.client.keyword_ideas.call_args.kwargs
    assert kw["location_name"] == "France" and kw["language_name"] == "French"
    assert kw["limit"] == 5


def test_list_ops_default_to_20_rows(env):
    env.call("dataforseo_domain", op="keywords", target="example.com")
    assert env.client.ranked_keywords.call_args.kwargs["limit"] == 20


def test_gap_defaults_to_shared_keywords(env):
    env.call("dataforseo_domain", op="gap", target="a.com", target2="b.com",
             location=2840, language="en")
    args = env.client.domain_intersection.call_args
    assert args.args == ("a.com", "b.com") and args.kwargs["intersections"] is True
    env.call("dataforseo_domain", op="gap", target="a.com", target2="b.com",
             location=2840, language="en", shared_only=False)
    assert env.client.domain_intersection.call_args.kwargs["intersections"] is False


@pytest.mark.parametrize("tool,kw,method", [
    ("dataforseo_keywords", dict(op="volume", keywords=["a"]), "google_ads_search_volume"),
    ("dataforseo_keywords", dict(op="intent", keywords=["a"]), "search_intent"),
    ("dataforseo_keywords", dict(op="suggestions", keyword="a"), "keyword_suggestions"),
    ("dataforseo_keywords", dict(op="related", keyword="a", location=2840, language="en"),
     "related_keywords"),
    ("dataforseo_keywords", dict(op="difficulty", keywords=["a"], location=2840,
                                 language="en"), "bulk_keyword_difficulty"),
    ("dataforseo_domain", dict(op="overview", target="a.com"), "domain_rank_overview"),
    ("dataforseo_domain", dict(op="pages", target="a.com"), "relevant_pages"),
    ("dataforseo_domain", dict(op="competitors", target="a.com", location=2840,
                               language="en"), "competitors_domain"),
    ("dataforseo_domain", dict(op="relevant_keywords", target="a.com", location=2840),
     "keywords_for_site"),
    ("dataforseo_domain", dict(op="traffic", targets=["a.com"]), "bulk_traffic_estimation"),
    ("dataforseo_backlinks", dict(op="list", target="a.com"), "backlinks_list"),
    ("dataforseo_backlinks", dict(op="referring_domains", target="a.com"),
     "referring_domains"),
    ("dataforseo_backlinks", dict(op="anchors", target="a.com"), "anchors"),
    ("dataforseo_backlinks", dict(op="ranks", targets=["a.com"]), "bulk_ranks"),
])
def test_each_op_reaches_its_method(env, tool, kw, method):
    env.call(tool, **kw)
    assert getattr(env.client, method).call_count == 1


# --- refusals before a paid call ----------------------------------------------------

@pytest.mark.parametrize("tool,kw,match", [
    ("dataforseo_keywords", dict(op="overview", keywords=["a"], language="en"), "location"),
    ("dataforseo_keywords", dict(op="overview", keywords=["a"], location=2840), "language"),
    ("dataforseo_domain", dict(op="gap", target="a.com", target2="b.com"), "location"),
    ("dataforseo_keywords", dict(op="nope"), "op must be"),
    ("dataforseo_keywords", dict(op="volume", keywords=["a"], depth=2), "depth"),
    ("dataforseo_keywords", dict(op="suggestions", keywords=["a"], keyword="a"), "keywords"),
    ("dataforseo_domain", dict(op="overview", target="a.com", limit=5), "limit"),
    ("dataforseo_domain", dict(op="keywords"), "target"),
    ("dataforseo_domain", dict(op="keywords", target="a.com", limit=5000), "limit"),
    ("dataforseo_backlinks", dict(op="summary", target="a.com", targets=["b"]), "targets"),
    ("dataforseo_backlinks", dict(op="anchors", target="a.com", mode="as_is"), "mode"),
])
def test_refused_without_spending(env, tool, kw, match):
    with pytest.raises(McpError, match=match):
        env.call(tool, **kw)
    assert env.traces == []


# --- billing ----------------------------------------------------------------------

def test_cost_is_traced_in_microdollars(env):
    env.client.backlinks_summary.return_value = _task(result=[{"rank": 3}], cost=0.02003)
    out = env.call("dataforseo_backlinks", op="summary", target="a.com")
    assert env.traces == [{"quantity": 20030}]
    assert out["cost_usd"] == 0.02003


def test_float_noise_does_not_overbill():
    assert dfs_tool._microdollars(3 * 1e-05) == 30
    assert dfs_tool._microdollars(None) == 0


def test_upstream_refusal_bills_nothing(env):
    from oto.tools.common.errors import UpstreamHTTPError
    env.client.backlinks_summary.side_effect = UpstreamHTTPError(
        402, {"status_code": 40200, "status_message": "Payment Required."},
        service="dataforseo")
    env.rc = SimpleNamespace(fields={"password": "a@b.c:pw"}, config={}, is_platform=True)
    with pytest.raises(McpError, match="shared DataForSEO balance is empty"):
        env.call("dataforseo_backlinks", op="summary", target="a.com")
    assert env.traces == []


# --- account ----------------------------------------------------------------------

def test_balance_of_own_key(env):
    env.client.user_data.return_value = _task(
        result=[{"login": "me@example.com", "money": {"balance": 41.5, "total": 50}}], cost=0)
    assert env.call("dataforseo_account", op="balance") == {
        "login": "me@example.com", "balance_usd": 41.5, "total_paid_usd": 50}


def test_balance_refused_on_the_shared_key(env):
    env.rc = SimpleNamespace(fields={"password": "a@b.c:pw"}, config={}, is_platform=True)
    with pytest.raises(McpError, match="shared key"):
        env.call("dataforseo_account", op="balance")
    env.client.user_data.assert_not_called()


def test_locations_filtered(env):
    env.client.labs_locations_and_languages.return_value = _task(result=[
        {"location_code": 2250, "location_name": "France", "location_type": "Country",
         "country_iso_code": "FR",
         "available_languages": [{"language_name": "French", "language_code": "fr",
                                  "keywords": 1}]},
        {"location_code": 2840, "location_name": "United States"}], cost=0)
    out = env.call("dataforseo_account", op="locations", query="fra")
    assert out["count"] == 1
    assert out["items"][0]["languages"] == [{"language_name": "French",
                                             "language_code": "fr"}]


# --- view -------------------------------------------------------------------------

def test_default_view_trims_and_full_does_not(env):
    raw = [{"se_type": "google", "total_count": 2, "items_count": 1, "offset_token": None,
            "items": [{"keyword": "crm", "keyword_info": {
                "search_volume": 100, "monthly_searches": [{"month": 1}], "cpc": None}}]}]
    env.client.ranked_keywords.return_value = _task(result=raw, cost=0.0102)
    out = env.call("dataforseo_domain", op="keywords", target="a.com")
    assert out == {"cost_usd": 0.0102, "total_count": 2, "items_count": 1,
                   "items": [{"keyword": "crm", "keyword_info": {"search_volume": 100}}]}
    out = env.call("dataforseo_domain", op="keywords", target="a.com", full=True)
    assert out == {"cost_usd": 0.0102, "result": raw}


def test_volume_rows_always_in_items(env):
    env.client.google_ads_search_volume.return_value = _task(
        result=[{"keyword": "crm", "search_volume": 90500, "monthly_searches": []}])
    out = env.call("dataforseo_keywords", op="volume", keywords=["crm"])
    assert out["items"] == [{"keyword": "crm", "search_volume": 90500}]


def test_verify_probe_uses_the_free_call():
    with patch("oto.tools.dataforseo.DataForSeoClient") as cls:
        dfs_tool._verify({"password": "pw"}, {"login": "me@example.com"})
    cls.assert_called_once_with(login="me@example.com", password="pw")
    cls.return_value.user_data.assert_called_once_with()

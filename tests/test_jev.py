"""Jev connector (TypeSafe via OpenRouter).

Locks: registry entry (no personal/platform key, no free tier), the tenant-key-only rule
(closer keys refused, naming who removes them), billing in real micro-dollars, the two
MCP tools, the verify probe, and batch behaviour (order, per-item errors, stop on key or
balance problems without losing billing, send window, state size cap).
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import jev

EXPECTED_TOOLS = {"jev_ask", "jev_items"}

NOUL = {"type": "noul", "instructions": "Does the condition hold?",
        "criteria": {"true": "yes", "false": "no"}}


def _answer(cost=2.0e-05, tokens=341):
    return {"model": "typesafe/jev-1.13-20260917",
            "answers": {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": tokens, "output_tokens": 21, "cost": cost}}


class _Rung:
    """What the cascade returns: the winning key and its rung."""

    def __init__(self, mode="tenant", key="sk-or-test"):
        self.mode = mode
        self.key = key
        self.is_platform = mode == "platform"


@pytest.fixture()
def monte(monkeypatch):
    """Mount the module with the client mocked INSIDE the patch (else `register()`
    captures the real class), the cascade on the tenant rung, and billing captured."""
    from fastmcp import FastMCP

    releve = {}

    def resoudre(provider, want="auto", **kw):
        releve["units"] = kw.get("units")
        return _Rung()

    monkeypatch.setattr("oto_mcp.access.resolve_credential", resoudre)
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **kw: releve.update(kw))
    with patch("oto.tools.jev.client.JevClient") as cls:
        cls.return_value.decide.return_value = _answer()
        m = FastMCP("t")
        jev.register(m)
        tools = {t.name: t for t in asyncio.run(m._list_tools())}
        yield m, cls.return_value, tools, releve


def _fn(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registry ---------------------------------------------------------------

def test_registry_no_personal_key_no_free_tier():
    c = providers.REGISTRY["jev"]
    assert c.kind == "tools" and c.keyed and c.secret_kind == "api_key"
    # No `byo_user` (not a personal key), no `platform` (the tenant brings the key).
    assert c.auth_modes == frozenset({"byo_org"})
    # No free tier: without a key the refusal is explicit.
    assert c.platform_key_open is False
    assert c.cardinality == "mono"
    assert "jev" in providers.KEY_PROVIDERS
    assert c.category == "Modèles" and c.publisher_name == "TypeSafe"


def test_org_shareable_because_it_is_the_tenant_rung_door():
    """⚠️ `byo_org` is required by `require_credential("tenant", …)`, not so orgs set keys."""
    assert providers.REGISTRY["jev"].org_shareable is True


def test_doc_how_to_present():
    kinds = {s.kind for s in providers.REGISTRY["jev"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------

def test_both_tools_mounted_with_description(monte):
    _, _, tools, _ = monte
    assert EXPECTED_TOOLS <= set(tools)
    for name in EXPECTED_TOOLS:
        assert tools[name].description, f"{name} has no description"
        assert namespace_of(name) == "jev"


def test_verify_probe_registered(monte):
    assert connector_verify.supports("jev")


def test_probe_fails_for_a_key_that_would_be_refused(monte):
    """A non-tenant key fails the probe WITHOUT a call (a green card would lie)."""
    with patch("oto.tools.jev.client.JevClient") as cls:
        with pytest.raises(ValueError, match="TENANT"):
            jev._verify({"key": "sk-or-x"}, {}, instance=("org", "7", ""))
        assert cls.call_count == 0
        jev._verify({"key": "sk-or-x"}, {}, instance=("tenant", "pilote", ""))
        jev._verify({"key": "sk-or-x"}, {})  # before saving: key only
        assert cls.return_value.decide.call_count == 2


# --- the rule: tenant key only ---------------------------------------------

@pytest.mark.parametrize("mode, who", [("org", "an org admin"),
                                       ("user", "its owner"),
                                       ("group", "a team admin"),
                                       ("platform", "whoever set it")])
def test_a_key_shadowing_the_tenant_key_is_refused_naming_who_removes_it(
        monte, monkeypatch, mode, who):
    m, client, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode=mode))
    with pytest.raises(McpError) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert mode in msg and "shadows" in msg and "TENANT" in msg and who in msg
    assert client.decide.call_count == 0


def test_tenant_rung_passes(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert r["answers"]["q"]["noul"] == 0.9
    assert r["model"] == "typesafe/jev-1.13-20260917"


def test_no_key_refusal_names_the_only_path(monte, monkeypatch):
    """The generic "set your own key" refusal is replaced by the tenant-key path."""
    from mcp.types import ErrorData
    from oto_mcp.access.resolve import CredentialUnavailable
    m, _, _, _ = monte

    def none_(provider, want="auto", **kw):
        raise CredentialUnavailable(ErrorData(code=-32602, message="Set your own key"))

    monkeypatch.setattr("oto_mcp.access.resolve_credential", none_)
    with pytest.raises(CredentialUnavailable) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert "TENANT" in msg and "Set your own key" not in msg


# --- billing ----------------------------------------------------------------

def test_billing_is_real_cost_in_microdollars(monte):
    m, client, _, releve = monte
    client.decide.return_value = _answer(cost=2.03e-05)
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    # 20.3 µ$ → 21: rounded UP, a paid call never bills 0.
    assert releve["quantity"] == 21


def test_batch_bills_the_sum_not_one_call(monte):
    m, client, _, releve = monte
    client.decide.return_value = _answer(cost=1.0e-05)
    _fn(m, "jev_items")([{"a": 1}, {"a": 2}, {"a": 3}], {"q": NOUL})
    # ⚠️ Regression: float sum 3.0000000000000004e-05 made bare `ceil` bill 31 µ$.
    assert releve["quantity"] == 30  # 3 × 10 µ$, not one call, not 31


def test_batch_checks_quota_for_all_items(monte):
    m, _, _, releve = monte
    _fn(m, "jev_items")([{"n": i} for i in range(7)], {"q": NOUL})
    assert releve["units"] == 7


def test_undeclared_cost_is_not_billed(monte):
    m, client, _, releve = monte
    client.decide.return_value = {"model": "x", "answers": {}, "usage": {}}
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert "quantity" not in releve


# --- batch behaviour ------------------------------------------------------

def test_batch_returns_answers_in_sent_order_with_caller_key(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_items")([{"key": "a", "state": {"n": 1}},
                             {"key": "b", "state": {"n": 2}}], {"q": NOUL})
    assert [x["index"] for x in r["answers"]] == [0, 1]
    assert [x["key"] for x in r["answers"]] == ["a", "b"]
    assert r["usage"]["decided"] == 2 and r["failed"] == 0


def test_a_refused_state_is_an_item_error_not_a_batch_error(monte):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    calls = {"n": 0}

    def decide(state, questions, model=None, **kw):
        calls["n"] += 1
        if state.get("n") == 2:
            raise UpstreamHTTPError(400, {"detail": {"error_type": "max_tokens_exceeded"}},
                                    service="jev")
        return _answer()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}, {"n": 3}], {"q": NOUL})
    assert r["failed"] == 1 and r["usage"]["decided"] == 2
    failed = [x for x in r["answers"] if "error" in x][0]
    assert failed["index"] == 1 and "max_tokens_exceeded" in failed["error"]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_key_or_balance_problem_stops_the_batch(monte, status):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    client.decide.side_effect = UpstreamHTTPError(status, {"message": "nope"}, service="jev")
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    # One message, not N copies.
    assert "key" in str(e.value) or "credits" in str(e.value)


def test_a_402_mid_batch_bills_what_was_already_paid(monte):
    """⚠️ Regression: the refusal escaped via `ex.map` and billing was never recorded."""
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] >= 3:
            raise UpstreamHTTPError(402, {"message": "no credits"}, service="jev")
        return _answer(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": i} for i in range(6)], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 30  # 3 paid answers, billed
    assert "3 answer(s) already given and billed" in str(e.value)


def test_unexpected_thread_exception_bills_then_raises(monte):
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] == 1:
            raise KeyError("unreadable body")
        return _answer(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(KeyError):
        _fn(m, "jev_items")([{"n": 0}, {"n": 1}], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 10


def test_after_the_window_unsent_states_go_to_retry(monte, monkeypatch):
    m, client, _, _ = monte
    monkeypatch.setattr(jev, "LOT_FENETRE_S", 0.2)

    def decide(state, questions, model=None, **kw):
        time.sleep(0.15)
        return _answer()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": i} for i in range(5)], {"q": NOUL}, parallel=1)
    assert r["usage"]["decided"] == 2
    assert r["retry"] == [2, 3, 4]
    assert r["failed"] == 3 and all("send it again" in x["error"]
                                    for x in r["answers"] if "error" in x)


def test_window_fits_under_rest_cap():
    # The worst last-moment call (10 s connect + read) ends before the 45 s REST cap.
    assert jev.LOT_FENETRE_S + 10 + jev.LECTURE_S < jev.REST_CALL_LIMIT_S


def test_complete_batch_has_nothing_to_retry(monte):
    m, _, _, _ = monte
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    assert r["retry"] == []


def test_oversized_state_refused_before_any_call(monte):
    m, client, _, _ = monte
    big = {"doc": "x" * (jev.MAX_ETAT_OCTETS + 1)}
    with pytest.raises(McpError, match="bytes"):
        _fn(m, "jev_ask")(big, {"q": NOUL})
    with pytest.raises(McpError, match=r"items\[1\]"):
        _fn(m, "jev_items")([{"n": 1}, big], {"q": NOUL})
    assert client.decide.call_count == 0


def test_malformed_parallel_is_a_named_error(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="parallel"):
        _fn(m, "jev_items")([{"n": 1}], {"q": NOUL}, parallel="lots")


def test_batch_is_bounded(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="max is"):
        _fn(m, "jev_items")([{"n": i} for i in range(jev.MAX_ITEMS + 1)], {"q": NOUL})
    with pytest.raises(McpError, match="at least one"):
        _fn(m, "jev_items")([], {"q": NOUL})


def test_rubric_checked_once_per_batch(monte):
    m, client, _, _ = monte
    client.check_questions.side_effect = ValueError("question 'q': `criteria` required")
    with pytest.raises(McpError, match="criteria"):
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": {"type": "noul"}})
    assert client.decide.call_count == 0

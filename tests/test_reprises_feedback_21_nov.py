"""Two refusals that were not failures (customer feedback, 17-21 Nov 2026).

- WhatsApp READS hit Unipile's API-wide burst limit ("We only allow 10 requests. Retry
  in 3 seconds"): 40 % of one account's reads. A short delay is now waited out once;
  a long one, a second refusal, and any SEND go back to the caller as is.
- `lemlist_add_lead_variables` refused every re-run ("Variables … already exist"):
  57 of 62 calls, as a raw HTTP 400. The add stays strict; the refusal now names the
  variables and points to `lemlist_lead(op="vars_update")`.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP


# --- WhatsApp -------------------------------------------------------------------

@pytest.fixture
def whatsapp(monkeypatch):
    from oto_mcp.tools import unipile as U, whatsapp as W

    cli = MagicMock()
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: cli)
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    m = FastMCP("t")
    W.register(m)
    lire = asyncio.run(m.get_tool("whatsapp")).fn
    agir = asyncio.run(m.get_tool("whatsapp_act")).fn
    return lire, agir, cli


def _limite(secs):
    from oto.tools.unipile.client import UnipileRateLimited
    return UnipileRateLimited(f"Unipile 429: We only allow 10 requests. Retry in {secs} seconds.",
                              retry_after=secs)


def test_whatsapp_read_waits_out_a_short_burst_limit(whatsapp):
    lire, _, cli = whatsapp
    cli.list_messages.side_effect = [_limite(3), {"items": []}]
    lire(op="read", chat_id="c1")
    assert cli.list_messages.call_count == 2


def test_whatsapp_list_waits_out_a_short_burst_limit(whatsapp):
    lire, _, cli = whatsapp
    cli.list_chats.side_effect = [_limite(4), {"items": []}]
    lire(op="list")
    assert cli.list_chats.call_count == 2


def test_whatsapp_long_delay_goes_back_to_the_caller(whatsapp):
    from oto.tools.unipile.client import UnipileRateLimited
    lire, _, cli = whatsapp
    cli.list_messages.side_effect = [_limite(17)]
    with pytest.raises(UnipileRateLimited):
        lire(op="read", chat_id="c1")
    assert cli.list_messages.call_count == 1


def test_whatsapp_second_refusal_goes_back_to_the_caller(whatsapp):
    from oto.tools.unipile.client import UnipileRateLimited
    lire, _, cli = whatsapp
    cli.list_messages.side_effect = [_limite(3), _limite(3)]
    with pytest.raises(UnipileRateLimited):
        lire(op="read", chat_id="c1")
    assert cli.list_messages.call_count == 2


def test_whatsapp_send_is_never_retried(whatsapp):
    from oto.tools.unipile.client import UnipileRateLimited
    _, agir, cli = whatsapp
    cli.send_message.side_effect = [_limite(3), {"ok": True}]
    with pytest.raises(UnipileRateLimited):
        agir(text="hi", chat_id="c1")
    assert cli.send_message.call_count == 1


# --- lemlist ---------------------------------------------------------------------

def _existe(*noms):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(
        400, {"ok": False, "status": f"Variables {', '.join(noms)} already exist"},
        service="lemlist")


def test_lemlist_existing_variables_are_a_named_refusal_not_an_overwrite():
    """The add stays an add: lemlist's refusal protects a value already set. It now
    names the variables and the tool that changes them, instead of a raw HTTP 400."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools.lemlist import _poser_variables

    cli = MagicMock()
    cli.add_lead_variables.side_effect = [_existe("subject_1", "email_1")]
    with pytest.raises(McpError) as e:
        _poser_variables(cli, "L1", {"subject_1": "S", "email_1": "E", "ps": "P"})
    msg = str(e.value)
    assert "['email_1', 'subject_1']" in msg
    assert 'lemlist_lead(op="vars_update", lead_id="L1"' in msg
    assert "['ps']" in msg, "the new names are pointed back to this tool"
    cli.update_lead_variables.assert_not_called()
    assert cli.add_lead_variables.call_count == 1


def test_lemlist_add_is_untouched_when_nothing_exists():
    from oto_mcp.tools.lemlist import _poser_variables

    cli = MagicMock()
    cli.add_lead_variables.return_value = {"raw": True}
    assert _poser_variables(cli, "L1", {"a": "1"}) == {"raw": True}


def test_lemlist_other_refusals_are_not_rewritten():
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.tools.lemlist import _poser_variables

    cli = MagicMock()
    cli.add_lead_variables.side_effect = [UpstreamHTTPError(404, {"error": "Lead not found"},
                                                            service="lemlist")]
    with pytest.raises(UpstreamHTTPError):
        _poser_variables(cli, "L1", {"a": "1"})

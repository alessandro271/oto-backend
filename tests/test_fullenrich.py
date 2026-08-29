"""FullEnrich — métrage par unité (billing Tulina, 21/08): `fullenrich_enrich_linkedin`
must trace the number of contacts SUBMITTED (not enriched/found — that count only
exists later, inside `fullenrich_result`, a separate call/journal row) via
`session_org.note_call_trace(quantity=…)`, regardless of platform vs BYO key —
unlike `access.record_platform_usage`, which only fires on the platform key and
serves a different purpose (oto's own internal quota, not org billing)."""
import asyncio
from unittest.mock import patch

import pytest


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import fullenrich

    m = FastMCP("t")
    fullenrich.register(m)
    return asyncio.run(m.get_tool(name))


def _contacts(n: int) -> list[dict]:
    return [{"first_name": f"F{i}", "last_name": f"L{i}", "linkedin_slug": f"f{i}-l{i}"}
           for i in range(n)]


@pytest.mark.parametrize("is_platform", [True, False])
def test_enrich_linkedin_traces_the_submitted_contact_count(is_platform):
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", is_platform)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_123"
        _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(7))

    # Unconditional — fires the SAME on a platform key and a BYO key, unlike
    # access.record_platform_usage (platform-only, a different mechanism/purpose).
    trace.assert_called_once_with(quantity=7)


def test_enrich_linkedin_traces_the_batch_size_not_a_fixed_value():
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_456"
        _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(100))

    trace.assert_called_once_with(quantity=100)


def test_enrich_linkedin_does_not_trace_on_a_rejected_submission():
    """A submit() failure (ValueError → McpError) must not leave a stale trace —
    nothing was actually billed against oto's own credits, so nothing should be
    billed against the org's either."""
    from oto_mcp.mcp_errors import McpError

    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.side_effect = ValueError("bad contact")
        with pytest.raises(McpError):
            _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))

    trace.assert_not_called()

"""WhatsApp — messaging hosted via Unipile (WhatsApp account connected by the user).

Formerly Baileys (self-hosted, Node.js subprocess) → replaced by Unipile: the
WhatsApp account lives at Unipile (linked-device), connected by the user via hosted-auth
(dashboard, `?channel=whatsapp`). Tools `whatsapp_chat(op=list|read)` (reads) and
`whatsapp_send` (sends, #1189) derived from the shared messaging factory (see
`tools/unipile.register_messaging_tools`). The Baileys engine
stays archived in oto-core (`oto.tools.whatsapp`) + the `oto whatsapp` CLI.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .unipile import register_messaging_tools


def register(mcp: FastMCP) -> None:
    register_messaging_tools(mcp, "WHATSAPP")

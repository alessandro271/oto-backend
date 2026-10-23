"""Google Chat — oto-core surface (ChatClient) exposed per user, multi-account.

List spaces (rooms + DMs), read messages, post (into a space or as a DM to a
user). **Restricted** scopes `chat.spaces.readonly` + `chat.messages`.
Default account or targeted by `account`. Per-user via OAuth.

**Surface** — the connector's PAIR (`tools/lecture.py`): `chat` READS (op=spaces: the
spaces, which PRODUCE the `space` the others consume; op=list: the messages of a space),
`chat_act` POSTS one (op=send). An MCP client authorizes a TOOL, never an op — "always
allow" on what reads must never allow a post. The old names (`chat_spaces`,
`chat_message`, `chat_send`) refuse, naming the call to make
(`outils_retires.RENOMMAGES`).

⚠️ `chat_act` really POSTS into a real Google Chat space, under the user's identity
(not a bot). Hence: an ambiguous destination (`space` AND `user`, or neither) is refused
instead of guessed, before any credential is resolved.
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth
from .lecture import ACTE, LECTURE

def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _http_error(e) -> McpError:
    """Normalizes a `googleapiclient.errors.HttpError` (raw unreadable stacktrace)
    into an actionable message, aligned with the messaging family (oto-backend#110).

    The 404 "Google Chat app not found" says nothing about the ACCOUNT: Google requires,
    for any write under the user's identity, that a Chat app be configured in the
    Google Cloud project of the OAuth client that issued the token (reads do without).
    It is the configuration of that client — ours, or that of a tenant that set up its
    own Google app —, not a user gesture: saying otherwise sends them to reconnect an
    account that has nothing to do with it (otomata-tech/oto#190)."""
    status = getattr(getattr(e, "resp", None), "status", None) or getattr(e, "status_code", None)
    detail = ""
    try:
        import json
        payload = json.loads(e.content.decode()) if getattr(e, "content", None) else {}
        detail = (payload.get("error") or {}).get("message") or ""
    # noqa: SILENT — non-JSON error body: the raw message is still returned
    except Exception:  # noqa: BLE001
        pass
    detail = detail or (getattr(e, "reason", None) or "").strip() or "unknown error"
    low = detail.lower()
    if status == 404 and ("app not found" in low or "chat api" in low or "turn on" in low):
        msg = ("Google Chat refuses to write: no Chat app is configured "
               "in the Google Cloud project of the OAuth client that issued this "
               "account's connection (Google requires it to write, not to read). It is a "
               "configuration of that OAuth client, to be done by its administrator in "
               "the Google Cloud console (Google Chat API → Configuration): reconnecting "
               f"the account or retrying changes nothing. Google detail: {detail}")
    else:
        msg = f"Google Chat refused the request (HTTP {status}): {detail}"
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


async def _call(fn, *args):
    """Runs a Chat client call off the loop + translates any `HttpError` into a clean
    error (never a raw stacktrace sent back to the agent)."""
    from googleapiclient.errors import HttpError
    try:
        return await asyncio.to_thread(fn, *args)
    except HttpError as e:
        raise _http_error(e)


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="chat")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.chat.lib.chat_client import ChatClient
    return ChatClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — see gmail.py::_client_for_user_async for the
# rationale (same token refresh mechanism, same method).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(token refresh) — retry.")


def register(mcp: FastMCP) -> None:

    @mcp.tool(name="chat", annotations=LECTURE)
    async def chat(
        op: Literal["spaces", "list"] = "spaces",
        space: Optional[str] = None,
        space_type: Optional[str] = None,
        max_results: Optional[int] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Reads Google Chat: the spaces the user belongs to, and the messages of one.
        Posting a message is `chat_act`.

        `op`:
        - **"spaces"** (default): the spaces (rooms + DMs) — {spaces: [{name, type,
          displayName, ...}], count}. A `name` ('spaces/XXXX') is the `space` of
          op="list" and of `chat_act`.
        - **"list"**: the recent messages of `space`, most recent first.

        Args:
            op: spaces (default) | list.
            space: op="list" — the space resource name ('spaces/XXXX'), from op="spaces".
            space_type: op="spaces" — optional filter, "SPACE" (rooms) or
                "DIRECT_MESSAGE" (DMs).
            max_results: cap on items returned (default 100 spaces, 20 messages).
            account: email of the Google account to use — same choice as `_account`.
        """
        # Refused BEFORE any credential resolution: an argument the op does not use is
        # an intent error, never ignored.
        if op == "spaces":
            if space is not None:
                raise _bad("op='spaces' does not use `space` — the messages of a space "
                           "are op='list'.")
            client = await _client_for_user_async(account)
            filter_ = f'spaceType = "{space_type}"' if space_type else None
            spaces = await _call(client.list_spaces, filter_,
                                 max_results if max_results is not None else 100)
            return {"spaces": spaces, "count": len(spaces)}
        if op != "list":
            raise _bad("op must be 'spaces' or 'list' — posting a message is `chat_act`.")
        if space_type is not None:
            raise _bad("op='list' does not use `space_type` — it filters op='spaces'.")
        if space is None:
            raise _bad("op='list' requires space ('spaces/XXXX', from op='spaces').")
        client = await _client_for_user_async(account)
        messages = await _call(client.list_messages, space,
                               max_results if max_results is not None else 20)
        return {"messages": messages, "count": len(messages)}

    @mcp.tool(name="chat_act", annotations=ACTE)
    async def chat_act(
        text: str,
        op: Literal["send"] = "send",
        space: Optional[str] = None,
        user: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """⚠️ POSTS a Google Chat message, under the user's own identity (not a bot) — it
        leaves at once and cannot be unsent from here. Reading is `chat`.

        `op`:
        - **"send"** (default): posts `text` — EITHER into `space` (a room/space) OR to
          `user` (a direct message), not both.

        Args:
            text: the message text (basic formatting: *bold*, _italic_).
            op: send (default).
            space: target space resource name ('spaces/XXXX'), from `chat(op="spaces")`.
            user: recipient email, sends a direct message (resolves the DM space). The
                DM space must ALREADY exist: Google Chat does not let a user create a
                brand-new DM space through the API — open the conversation once in
                Chat, then retry.
            account: email of the Google account to use — same choice as `_account`.
        """
        # Refused BEFORE any credential resolution: a post without a destination or a
        # text never reaches Google.
        if bool(space) == bool(user):
            raise _bad("chat_act op='send' requires either `space` (message in a space) "
                       "or `user` (DM), not both and not neither.")
        if not text:
            raise _bad("chat_act op='send' requires text")
        client = await _client_for_user_async(account)
        if user:
            return await _call(client.send_dm, user, text)
        return await _call(client.send, space, text)

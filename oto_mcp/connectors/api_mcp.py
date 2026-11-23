"""A Custom API whose `protocol` is `mcp`: the card points at a remote MCP server
(Streamable HTTP) instead of a REST API, and the agent reaches its tools by name.

This is NOT federation (`kind="mount"`, retired by ADR 0069): nothing is mounted in the
catalogue, no remote tool becomes an oto tool. The remote server is one Custom API card
like any other — its address, its auth (any mode the card allows, the per-member
`oauth2_user` sign-in included), its keys at every tier — and two verbs reach it:

- `api(op="mcp_tools")` — the remote tools (name, description, input schema), filtered
  by the definition's `mcp_tools` allowlist when the admin set one;
- `api_act(op="mcp_call")` — one remote tool, by name, with its arguments; refused by
  name when the allowlist does not carry it.

One short session per verb: `initialize` → `notifications/initialized` → the request →
`DELETE` of the session (best effort). The answer may be JSON or an SSE stream; only the
JSON-RPC answer to OUR id is read. Every request goes out under the egress guard, with
no redirect followed (a redirect would carry the key to a host nobody chose), and the
body read is bounded.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

from .. import egress

logger = logging.getLogger(__name__)

PROTOCOL_FIELD = "protocol"
MCP = "mcp"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 45
MAX_BODY = 4 * 1024 * 1024
MAX_PAGES = 10
#: Modes that sign in with a person's password or a visitor's cookies: not for MCP.
_MODES_REFUSES = ("session", "visitor")


class McpRefused(ValueError):
    """A named refusal, safe to show (never a secret, never a URL with a query)."""

    def __init__(self, code: str, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def is_mcp(fields: dict) -> bool:
    return (fields.get(PROTOCOL_FIELD) or "").strip().lower() == MCP


def allowlist(definition: dict) -> Optional[list[str]]:
    """The remote tools the admin opened to agents, or None = all of them."""
    names = (definition or {}).get("mcp_tools")
    return list(names) if isinstance(names, list) and names else None


# --- the authenticated session -------------------------------------------------

def _session(fields: dict, row) -> requests.Session:
    from oto.tools.http import build_auth

    mode = (fields.get("auth_mode") or "").strip().lower()
    if mode in _MODES_REFUSES:
        raise McpRefused("mcp_mode_unsupported",
                         f"A Custom API in `{mode}` mode cannot reach an MCP server: use "
                         "a token, a header key, OAuth2 or the per-member sign-in "
                         "(`oauth2_user`).")
    s = requests.Session()
    s.max_redirects = 0
    if mode == "oauth2_user":
        from ..auth import api_oauth
        if row is None:
            raise McpRefused("mcp_no_row", "This call has no vault row to renew its "
                             "sign-in on.")
        try:
            token = api_oauth.access_token_for(row, fields)
        except api_oauth.ApiOAuthRefused as e:
            raise McpRefused(e.code, e.message) from None
        s.headers["Authorization"] = f"Bearer {token}"
        return s
    if mode == "oauth2":
        egress.check_url(fields.get("token_url") or "", connector="api", field="token_url")
    auth = build_auth(mode, fields)
    auth.configure(s)
    q = auth.query_params()
    if q:
        s.params = q          # query mode: the key rides as its parameter
    return s


# --- JSON-RPC over Streamable HTTP ---------------------------------------------

def _lire(r: requests.Response) -> bytes:
    out = bytearray()
    for chunk in r.iter_content(65536):
        out += chunk
        if len(out) > MAX_BODY:
            raise McpRefused("mcp_answer_too_large",
                             f"The MCP server answered more than {MAX_BODY // 1048576} MB.")
    return bytes(out)


def _messages(r: requests.Response, corps: bytes) -> list[dict]:
    """The JSON-RPC messages of an answer: a JSON body (one or a batch), or the `data:`
    of each event of an SSE stream."""
    ctype = (r.headers.get("Content-Type") or "").lower()
    texte = corps.decode("utf-8", errors="replace")
    blocs: list[str] = []
    if "text/event-stream" in ctype:
        courant: list[str] = []
        for ligne in texte.splitlines():
            if ligne.startswith("data:"):
                courant.append(ligne[5:].lstrip())
            elif not ligne.strip() and courant:
                blocs.append("\n".join(courant))
                courant = []
        if courant:
            blocs.append("\n".join(courant))
    elif texte.strip():
        blocs.append(texte)
    out: list[dict] = []
    for b in blocs:
        try:
            v = json.loads(b)
        except ValueError:
            continue
        out += [m for m in (v if isinstance(v, list) else [v]) if isinstance(m, dict)]
    return out


class _Client:
    def __init__(self, url: str, session: requests.Session):
        self.url, self.s = url, session
        self.sid: Optional[str] = None
        self.n = 0

    def _post(self, payload: dict) -> requests.Response:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            r = self.s.post(self.url, data=json.dumps(payload), headers=h, timeout=TIMEOUT,
                            stream=True, allow_redirects=False)
        except requests.TooManyRedirects:
            raise McpRefused("mcp_redirect", "The MCP server answered with a redirect: "
                             "set the card's base URL to the server's final address.")
        except requests.RequestException as e:
            raise McpRefused("mcp_unreachable",
                             f"The MCP server could not be reached ({type(e).__name__}).")
        if 300 <= r.status_code < 400:
            raise McpRefused("mcp_redirect", "The MCP server answered with a redirect: "
                             "set the card's base URL to the server's final address.")
        if r.status_code in (401, 403):
            raise McpRefused("mcp_unauthorized",
                             f"The MCP server refused the key (HTTP {r.status_code}).",
                             status=r.status_code)
        return r

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        self.n += 1
        rid = self.n
        r = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params or {}})
        corps = _lire(r)
        if r.status_code >= 400:
            raise McpRefused("mcp_http_error",
                             f"The MCP server answered HTTP {r.status_code} to `{method}`.",
                             status=r.status_code)
        if method == "initialize":
            self.sid = r.headers.get("Mcp-Session-Id") or None
        for m in _messages(r, corps):
            if m.get("id") != rid:
                continue
            if "error" in m:
                err = m["error"] if isinstance(m["error"], dict) else {}
                raise McpRefused("mcp_error",
                                 f"The MCP server refused `{method}`: "
                                 f"{str(err.get('message') or 'error')[:500]}")
            return m.get("result")
        raise McpRefused("mcp_no_answer",
                         f"The MCP server sent no answer to `{method}`.")

    def notify(self, method: str) -> None:
        r = self._post({"jsonrpc": "2.0", "method": method})
        _lire(r)

    def close(self) -> None:
        if not self.sid:
            return
        try:
            self.s.delete(self.url, headers={"Mcp-Session-Id": self.sid},
                          timeout=10, allow_redirects=False)
        # noqa: SILENT — fin de session de courtoisie : le serveur l'expire seul
        except requests.RequestException:
            logger.info("api mcp: session close not acknowledged")


def _ouvrir(fields: dict, row) -> _Client:
    url = (fields.get("base_url") or "").strip()
    if not url:
        raise McpRefused("mcp_no_url", "The Custom API card has no base URL.")
    egress.check_url(url, connector="api", field="base_url")
    c = _Client(url, _session(fields, row))
    c.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                             "clientInfo": {"name": "oto-custom-api", "version": "1"}})
    c.notify("notifications/initialized")
    return c


def _une_fois(fields: dict, row, fn):
    """`fn(client)` in one session; a refused per-member token is dropped and the call
    replayed ONCE with a fresh one."""
    for tentative in (0, 1):
        c = None
        try:
            c = _ouvrir(fields, row)
            return fn(c)
        except McpRefused as e:
            mode = (fields.get("auth_mode") or "").strip().lower()
            if e.code == "mcp_unauthorized" and mode == "oauth2_user" and tentative == 0 \
                    and row is not None:
                from ..auth import api_oauth
                api_oauth.oublier(row)
                continue
            raise
        finally:
            if c is not None:
                c.close()
    raise AssertionError("unreachable")


# --- the two verbs ---------------------------------------------------------------

def _resume(t: dict) -> dict:
    out = {"name": t.get("name"), "description": t.get("description"),
           "input_schema": t.get("inputSchema") or {"type": "object"}}
    if isinstance(t.get("annotations"), dict):
        out["annotations"] = t["annotations"]
    return out


def list_tools(fields: dict, row, definition: dict) -> dict:
    ouverts = allowlist(definition)

    def run(c: _Client) -> dict:
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            res = c.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += [t for t in (res.get("tools") or []) if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        vus = [_resume(t) for t in tools if not ouverts or t.get("name") in ouverts]
        return {"tools": vus, "count": len(vus),
                "hidden": len(tools) - len(vus) if ouverts else 0}
    return _une_fois(fields, row, run)


def call_tool(fields: dict, row, definition: dict, name: Optional[str],
              arguments: Optional[dict]) -> dict:
    if not name or not isinstance(name, str):
        raise McpRefused("mcp_tool_required",
                         "`api_act(op=\"mcp_call\")` needs `operation`: the remote tool's "
                         "name, from `api(op=\"mcp_tools\")`.")
    ouverts = allowlist(definition)
    if ouverts and name not in ouverts:
        raise McpRefused("mcp_tool_not_allowed",
                         f"The remote tool `{name}` is not open to agents on this Custom "
                         f"API (allowed: {', '.join(ouverts[:30])}). An org admin opens "
                         "it in the definition's `mcp_tools`.")
    if arguments is not None and not isinstance(arguments, dict):
        raise McpRefused("mcp_bad_arguments", "`args` must be an object.")

    def run(c: _Client) -> dict:
        res = c.request("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        out = {"tool": name, "is_error": bool(res.get("isError")),
               "content": res.get("content") or []}
        if "structuredContent" in res:
            out["structured"] = res["structuredContent"]
        return out
    return _une_fois(fields, row, run)

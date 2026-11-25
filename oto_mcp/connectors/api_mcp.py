"""A Custom API whose `protocol` is `mcp`: the card points at a remote MCP server
(Streamable HTTP) instead of a REST API, and the agent reaches its tools by name.

This is NOT federation (`kind="mount"`, retired by ADR 0069): nothing is mounted in the
catalogue, no remote tool becomes an oto tool. The remote server is one Custom API card
like any other — its address, its auth (any mode the card allows, the per-member
`oauth2_user` sign-in included), its keys at every tier — and two verbs reach it:

- `api(op="mcp_tools")` — the remote tools (name, description, input schema), filtered
  by the definition's `mcp_tools` allowlist when the admin set one;
- `api_act(op="call_mcp")` — one remote tool, by name, with its arguments; refused by
  name when the allowlist does not carry it.

One short session per verb: `initialize` → `notifications/initialized` → the request →
`DELETE` of the session (best effort). Every request after the `initialize` carries the
negotiated `MCP-Protocol-Version`, with or without a session id. The answer may be JSON
or an SSE stream; only the JSON-RPC answer to OUR id is read. What the remote server
writes (tool descriptions, text content, structured content, error messages) reaches
the agent fenced as third-party data (`texte_tiers.cloture`): descriptions, input
schemas, annotations and every content block, each bounded (`MAX_DESCRIPTION`,
`MAX_SCHEMA`, `MAX_TOOLS`, `MAX_SERVED`); a remote tool whose name is not a plain
identifier (`NOM_OUTIL`) is never served nor called. Every request — `initialize`, the
notification, the request, the session's `DELETE` — goes out on a session pinned by the
egress guard (`egress_transport.harden`: the host is resolved once and checked at each
connection), with no redirect followed (a redirect would carry the key to a host nobody
chose), and the body read is bounded.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from .. import egress, egress_transport
from ..texte_tiers import cloture

logger = logging.getLogger(__name__)

PROTOCOL_FIELD = "protocol"
MCP = "mcp"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 45
MAX_BODY = 4 * 1024 * 1024
MAX_PAGES = 10
#: What reaches the agent, at most: tools listed, characters of one description, of one
#: tool's input schema (as JSON), and of a whole answer (list or call).
MAX_TOOLS = 200
MAX_DESCRIPTION = 2000
MAX_SCHEMA = 8000
MAX_SERVED = 256 * 1024
#: A remote tool's name: a plain identifier, or it is neither listed nor called.
NOM_OUTIL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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
    s = egress_transport.harden(requests.Session(), connector="api", field="base_url")
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


def _tiers(balise: str, texte: str, origine: str) -> str:
    return cloture(balise, texte, origine=origine, lecture="data")


class _Client:
    def __init__(self, url: str, session: requests.Session):
        self.url, self.s = url, session
        self.sid: Optional[str] = None
        #: The protocol version the `initialize` settled on; sent on every request after.
        self.version: Optional[str] = None
        self.n = 0

    def _entetes(self) -> dict:
        h: dict = {}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        if self.version:
            h["MCP-Protocol-Version"] = self.version
        return h

    def _post(self, payload: dict) -> requests.Response:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", **self._entetes()}
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
                raise McpRefused("mcp_error", f"The MCP server refused `{method}`."
                                 + _tiers("remote-mcp-error",
                                          str(err.get("message") or "error")[:500],
                                          "Message written by the remote MCP server"))
            res = m.get("result")
            if method == "initialize":
                v = res.get("protocolVersion") if isinstance(res, dict) else None
                self.version = v if isinstance(v, str) and v else PROTOCOL_VERSION
            return res
        raise McpRefused("mcp_no_answer",
                         f"The MCP server sent no answer to `{method}`.")

    def notify(self, method: str) -> None:
        r = self._post({"jsonrpc": "2.0", "method": method})
        _lire(r)

    def close(self) -> None:
        if not self.sid:
            return
        try:
            self.s.delete(self.url, headers=self._entetes(),
                          timeout=10, allow_redirects=False)
        # noqa: SILENT — fin de session de courtoisie : le serveur l'expire seul ; un
        # refus d'egress à la connexion n'envoie rien, il ne masque pas l'issue du verbe
        except (requests.RequestException, egress.EgressRefused):
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

def _borne(texte: str, n: int) -> str:
    return texte if len(texte) <= n else texte[:n] + f" […truncated at {n} characters]"


def _json(valeur) -> str:
    try:
        return json.dumps(valeur, ensure_ascii=False, indent=1, sort_keys=False)
    except (TypeError, ValueError):
        return str(valeur)


def nom_valide(name) -> bool:
    return isinstance(name, str) and bool(NOM_OUTIL.match(name))


def _resume(t: dict) -> dict:
    """One remote tool as served: its name (already checked), and everything else it
    wrote fenced and bounded — the description, the input schema (its titles,
    descriptions and enums are the server's prose too), the annotations."""
    desc = t.get("description")
    out = {"name": t["name"],
           "description": _tiers("remote-mcp-tool-description",
                                 _borne(str(desc), MAX_DESCRIPTION),
                                 "Tool description written by the remote MCP server")
           if desc else None,
           "input_schema": cloture(
               "remote-mcp-tool-schema",
               _borne(_json(t.get("inputSchema") or {"type": "object"}), MAX_SCHEMA),
               origine="Input schema written by the remote MCP server",
               lecture="data (a JSON Schema)")}
    if isinstance(t.get("annotations"), dict):
        out["annotations"] = cloture(
            "remote-mcp-tool-annotations", _borne(_json(t["annotations"]), MAX_DESCRIPTION),
            origine="Tool annotations written by the remote MCP server",
            lecture="data (JSON)")
    return out


def _contenu_cloture(bloc, reste: int = MAX_SERVED):
    """A content block of a remote tool's answer, fenced as third-party data, within
    `reste` characters: a `text` block keeps its type with its text fenced; any other
    block (an embedded resource, a `resource_link`, an image…) is served whole as fenced
    JSON — its uri, name, title, description and data are the server's words too."""
    origine = "Content returned by the remote MCP server"
    if isinstance(bloc, dict) and bloc.get("type") == "text" \
            and isinstance(bloc.get("text"), str):
        return {"type": "text",
                "text": _tiers("remote-mcp-content", _borne(bloc["text"], reste), origine)}
    genre = bloc.get("type") if isinstance(bloc, dict) else None
    return {"type": genre if nom_valide(genre) else "unknown",
            "fenced": cloture("remote-mcp-content", _borne(_json(bloc), reste),
                              origine=origine, lecture="data (JSON)")}


def _structure_cloture(valeur, reste: int = MAX_SERVED) -> str:
    """The remote tool's `structuredContent`, served as fenced third-party data: its
    JSON, indented, inside the same named block as its text content. Any string in it
    is the remote server's prose, so the object as a whole is fenced — the agent still
    reads it as JSON, with no tag to strip and no key renamed."""
    return cloture("remote-mcp-structured", _borne(_json(valeur), reste),
                   origine="Structured content returned by the remote MCP server",
                   lecture="data (JSON)")


def list_tools(fields: dict, row, definition: dict) -> dict:
    ouverts = allowlist(definition)

    def run(c: _Client) -> dict:
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            res = c.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += [t for t in (res.get("tools") or []) if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS * 4:
                break
        nommes = [t for t in tools if nom_valide(t.get("name"))]
        ouverts_ici = [t for t in nommes if not ouverts or t["name"] in ouverts]
        vus: list[dict] = []
        taille, tronque = 0, False
        for t in ouverts_ici:
            r = _resume(t)
            poids = len(_json(r))
            if len(vus) >= MAX_TOOLS or taille + poids > MAX_SERVED:
                tronque = True
                break
            vus.append(r)
            taille += poids
        out = {"tools": vus, "count": len(vus),
               "hidden": len(nommes) - len(ouverts_ici) if ouverts else 0}
        if len(nommes) < len(tools):
            out["refused_names"] = len(tools) - len(nommes)
        if tronque or cursor:
            out["truncated"] = True
        return out
    return _une_fois(fields, row, run)


def call_tool(fields: dict, row, definition: dict, name: Optional[str],
              arguments: Optional[dict]) -> dict:
    if not name or not isinstance(name, str):
        raise McpRefused("mcp_tool_required",
                         "`api_act(op=\"call_mcp\")` needs `operation`: the remote tool's "
                         "name, from `api(op=\"mcp_tools\")`.")
    if not nom_valide(name):
        raise McpRefused("mcp_tool_name_invalid",
                         "A remote tool's name is 1 to 64 letters, digits, `_`, `.` or "
                         "`-`: this one is not called.")
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
        blocs = res.get("content") or []
        blocs = blocs if isinstance(blocs, list) else []
        contenu: list = []
        reste = MAX_SERVED
        for b in blocs:
            if reste <= 0:
                break
            servi = _contenu_cloture(b, reste)
            contenu.append(servi)
            reste -= len(_json(servi))
        out = {"tool": name, "is_error": bool(res.get("isError")), "content": contenu}
        if "structuredContent" in res and reste > 0:
            out["structured"] = _structure_cloture(res["structuredContent"], reste)
            reste -= len(out["structured"])
        if len(contenu) < len(blocs) or reste <= 0:
            out["truncated"] = True
        return out
    return _une_fois(fields, row, run)

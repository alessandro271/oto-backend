"""How a Custom API app signs in to a remote MCP server the way Claude does: with no key pasted
anywhere (MCP authorization spec — RFC 9728, RFC 8414, Client ID Metadata Documents,
RFC 7591).

For a Custom API app whose protocol is `mcp` and whose mode is `oauth2_user`, the admin
gives the server's URL and nothing else. At the first "Connect" of a member:

1. `discover(url)` — an unauthenticated `initialize` answers 401 with
   `WWW-Authenticate: Bearer resource_metadata="…"` (else the well-known paths of RFC
   9728 are tried): the protected-resource metadata names the `resource` and its
   authorization server; that server's metadata (RFC 8414, else OpenID discovery) gives
   the authorization and token endpoints, and how a client may identify itself;
2. `client_id_for(meta)` — a server that accepts Client ID Metadata Documents gets the
   URL of OURS (`client_document`, served at `CLIENT_DOCUMENT_PATH`: public client, PKCE,
   our callback as redirect URI — no secret, nothing registered); else a server with a
   `registration_endpoint` gets a dynamic registration (RFC 7591, public client); else
   the admin sets `oauth_client_id` on the app, and the refusal says so;
3. what was found is written on the APP (only the fields the admin left empty), so every
   member after the first connects without discovery, and with the same client.

Every request goes out under the egress guard, without following a redirect; every
document read is bounded and must be JSON.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from .. import egress
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.mcp_discovery")

CLIENT_DOCUMENT_PATH = "/api/custom-api/oauth/client.json"
CLIENT_NAME = "Custom API"
_TIMEOUT = 15
_MAX = 256 * 1024
_RM = re.compile(r'resource_metadata="([^"]+)"')


class DiscoveryRefused(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _https(url: str, what: str) -> str:
    p = urlsplit(url or "")
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise DiscoveryRefused("mcp_discovery_failed",
                               f"The {what} the MCP server announced is not an https "
                               "address: nothing was followed.")
    egress.check_url(url, connector="api", field=what.replace(" ", "_"))
    return url


def _get_json(url: str, what: str) -> Optional[dict]:
    _https(url, what)
    try:
        r = requests.get(url, timeout=_TIMEOUT, allow_redirects=False,
                         headers={"Accept": "application/json"}, stream=True)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    corps = bytearray()
    for chunk in r.iter_content(65536):
        corps += chunk
        if len(corps) > _MAX:
            return None
    try:
        v = json.loads(corps)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _well_known(base: str, name: str) -> list[str]:
    """RFC 8414 / 9728 insertion: `https://h/.well-known/<name>/<path>`, then the root."""
    p = urlsplit(base)
    chemin = p.path.rstrip("/")
    out = []
    if chemin:
        out.append(urlunsplit((p.scheme, p.netloc, f"/.well-known/{name}{chemin}", "", "")))
    out.append(urlunsplit((p.scheme, p.netloc, f"/.well-known/{name}", "", "")))
    return out


def _resource_metadata_url(mcp_url: str) -> list[str]:
    """Where the server says its metadata lives (the 401's header), then RFC 9728's."""
    urls: list[str] = []
    try:
        r = requests.post(mcp_url, timeout=_TIMEOUT, allow_redirects=False,
                          headers={"Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18",
                                                      "capabilities": {},
                                                      "clientInfo": {"name": "oto-custom-api",
                                                                     "version": "1"}}}))
        if r.status_code == 401:
            m = _RM.search(r.headers.get("WWW-Authenticate") or "")
            if m:
                urls.append(m.group(1))
        elif r.status_code < 300:
            raise DiscoveryRefused("mcp_no_auth_needed",
                                   "This MCP server answered without asking for a "
                                   "sign-in: set the app's mode to `none`.")
    except requests.RequestException:
        raise DiscoveryRefused("mcp_unreachable", "The MCP server could not be reached.")
    return urls + _well_known(mcp_url, "oauth-protected-resource")


def discover(mcp_url: str) -> dict:
    """`{resource, issuer, authorize_url, token_url, scope, registration_endpoint, cimd}`
    of an MCP server, or `DiscoveryRefused` naming the step that failed."""
    _https(mcp_url, "base url")
    prm = None
    for u in _resource_metadata_url(mcp_url):
        prm = _get_json(u, "resource metadata")
        if prm and prm.get("authorization_servers"):
            break
    if not prm or not prm.get("authorization_servers"):
        raise DiscoveryRefused("mcp_discovery_failed",
                               "The MCP server publishes no protected-resource metadata "
                               "(RFC 9728): an org admin fills the app's authorization "
                               "URL, token URL and client ID by hand.")
    # RFC 9728 §3.3: the metadata must be about THIS server — or a server could announce
    # another one's resource (ours) and receive members' tokens for it.
    if str(prm.get("resource") or "").rstrip("/") != mcp_url.rstrip("/"):
        raise DiscoveryRefused("mcp_discovery_failed",
                               "The MCP server's metadata describes another resource than "
                               "the app's address: nothing was followed.")
    issuer = str(prm["authorization_servers"][0]).rstrip("/")
    asm = None
    for u in [*_well_known(issuer, "oauth-authorization-server"),
              f"{issuer}/.well-known/openid-configuration",
              *_well_known(issuer, "openid-configuration")]:
        asm = _get_json(u, "authorization server metadata")
        # RFC 8414 §3.3: the metadata must name the issuer it was looked up for. One
        # tolerance: an MCP server that names ITSELF as authorization server and
        # republishes its identity provider's metadata (a common front for a hosted
        # Logto / Auth0) — the server already vouches for its own sign-in. The resource
        # check above is what keeps another server's tokens out of reach.
        if asm and str(asm.get("issuer") or "").rstrip("/") != issuer and not (
                urlsplit(issuer).hostname == urlsplit(mcp_url).hostname):
            asm = None
            continue
        if asm and asm.get("authorization_endpoint") and asm.get("token_endpoint"):
            break
    if not asm or not asm.get("authorization_endpoint") or not asm.get("token_endpoint"):
        raise DiscoveryRefused("mcp_discovery_failed",
                               f"The sign-in server {urlsplit(issuer).hostname} publishes "
                               "no usable metadata (RFC 8414).")
    methods = asm.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise DiscoveryRefused("mcp_discovery_failed",
                               "The sign-in server does not support PKCE S256.")
    scopes = [s for s in (prm.get("scopes_supported") or []) if isinstance(s, str)]
    if "offline_access" in (asm.get("scopes_supported") or []) and \
            "offline_access" not in scopes:
        scopes.append("offline_access")
    return {"resource": str(prm["resource"]),
            "issuer": issuer,
            "authorize_url": _https(asm["authorization_endpoint"], "authorization url"),
            "token_url": _https(asm["token_endpoint"], "token url"),
            "scope": " ".join(scopes),
            "registration_endpoint": asm.get("registration_endpoint") or None,
            "cimd": bool(asm.get("client_id_metadata_document_supported"))}


# --- our client identity -------------------------------------------------------

def client_document_url() -> str:
    return oauth_flow.redirect_uri(CLIENT_DOCUMENT_PATH)


def client_document() -> dict:
    """OUR Client ID Metadata Document: the `client_id` is its own URL."""
    from . import api_oauth
    return {"client_id": client_document_url(), "client_name": CLIENT_NAME,
            "redirect_uris": [api_oauth.redirect()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}


def _register(endpoint: str) -> str:
    from . import api_oauth
    _https(endpoint, "registration endpoint")
    try:
        r = requests.post(endpoint, timeout=_TIMEOUT, allow_redirects=False,
                          json={"client_name": CLIENT_NAME,
                                "redirect_uris": [api_oauth.redirect()],
                                "grant_types": ["authorization_code", "refresh_token"],
                                "response_types": ["code"],
                                "token_endpoint_auth_method": "none"})
        body = r.json() if r.status_code in (200, 201) else {}
    except (requests.RequestException, ValueError):
        body = {}
    cid = body.get("client_id") if isinstance(body, dict) else None
    if not isinstance(cid, str) or not cid:
        raise DiscoveryRefused("mcp_registration_refused",
                               "The sign-in server refused to register oto as a "
                               "client: an org admin sets the app's client ID by hand.")
    return cid


def client_id_for(meta: dict) -> str:
    if meta.get("cimd"):
        return client_document_url()
    if meta.get("registration_endpoint"):
        return _register(meta["registration_endpoint"])
    raise DiscoveryRefused("mcp_client_required",
                           "The sign-in server accepts neither a client metadata document "
                           "nor a registration: an org admin creates a public client there "
                           "(PKCE, no secret) and sets its ID on the app.")


#: What discovery fills on an app, when the admin left it empty.
FILLED = ("authorize_url", "token_url", "oauth_client_id", "scope", "resource")


def complete_app(app: dict, by: Optional[str]) -> dict:
    """The app with its sign-in coordinates complete: discovered and written once when
    any is missing (only the empty ones), else returned unchanged."""
    from ..db import api_apps as store

    conn = dict(app.get("connection") or {})
    if all(conn.get(k) for k in ("authorize_url", "token_url", "oauth_client_id")):
        return app
    meta = discover(conn.get("base_url") or "")
    found = {"authorize_url": meta["authorize_url"], "token_url": meta["token_url"],
             "scope": meta["scope"], "resource": meta["resource"]}
    if not conn.get("oauth_client_id"):
        found["oauth_client_id"] = client_id_for(meta)
    for k, v in found.items():
        if v and not conn.get(k):
            conn[k] = v
    saved = store.modifier(int(app["id"]), connection=conn, by=by)
    logger.info("custom api: MCP sign-in discovered for app #%s (%s)", app["id"],
                urlsplit(meta["issuer"]).hostname)
    return saved or {**app, "connection": conn}

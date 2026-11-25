"""Custom API apps in `oauth2_user` mode — each MEMBER signs in at the API's own
authorization server (OAuth 2.0 authorization code + PKCE S256), and the API applies its
own rights to that person.

Before this mode a Custom API reached a service with ONE identity: a key, a basic pair, a
client-credentials client. An internal API that applies rights per user (a CRM whose
sales only see their accounts) could only be reached as an admin, or not at all. Here:

1. an org admin creates the app in `oauth2_user` mode (`connectors/api_apps.py`): the
   API's base URL, its sign-in server's authorization and token endpoints, the PUBLIC
   client registered for oto there (no secret: native / SPA client type, PKCE), the
   scopes and, when the server issues API tokens per resource, the resource indicator
   (RFC 8707). Nothing secret lives on the app;
2. a member clicks "Connect" on the app's card (`custom_api.apps.connect`): the browser
   goes to the sign-in server with a signed, single-use `state` carrying the person, the
   org, the app and the PKCE verifier — sealed, readable by this server only;
3. the server sends the browser back to `CALLBACK_PATH`; the callback hands the code to
   the front, which completes with its own token (`auth/flow.py`, card `api`). The code
   becomes a refresh token, stored as the member's KEY of the app (member tier, under the
   app's account, bound by `meta.api_app` exactly like a key set by hand);
4. at call time `tools/api.client_for` asks `access_token_for` for a valid access token
   and calls the API as a `bearer` — oto-core never sees this mode.

**One sign-in server.** `authorize_url` and `token_url` are on ONE origin — the
discovered issuer's when discovery read it, else the one the admin declared — and, for an
MCP server, `resource` is the server's own address (`api_apps.refus_d_origine`). This is
checked when the app is written AND re-read at each of the three requests (authorization,
exchange, renewal): an app that breaks it refuses (`api_app_origin_mixed`). The return's
`iss` (RFC 9207) is sealed by the callback with the code, and checked before the
exchange: required and equal to the recorded issuer when the server announced it, and a
present `iss` that differs is refused in every case.

**Renewal**: the token request goes out with NO transaction open (a slow sign-in server
never holds a database lock); calls of one process for one row are serialised in memory,
so they share one renewal. Only the write of a rotated refresh token takes the advisory
lock on the vault ROW, re-reads it and writes only if the row still holds the refresh
token the request was made with — a token changed meanwhile (a reconnection, another
process's renewal) is kept, never overwritten. A dead grant (`invalid_grant`) is
re-checked against the row before it marks anything — a refresh token that changed
meanwhile is a reconnection, retried once. A dead grant MARKS the row (`health_ko`),
never purges it; any other refusal (client unknown, server error) marks nothing. The
access token never goes to the database: a process cache keyed by the hash of the row
and its refresh token.

Every request goes out on a session pinned by the egress guard (`egress_transport`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional
from urllib.parse import urlencode, urlsplit

import requests

from .. import credentials_store, egress, egress_transport
from ..connectors import health as connector_health
from ..texte_tiers import cloture
from . import flow as oauth_flow
from . import pkce

logger = logging.getLogger("oto_mcp.auth.api_oauth")

CONNECTOR = "api"
MODE = "oauth2_user"
_AUD = "custom_api"
CALLBACK_PATH = "/api/custom-api/oauth/callback"
_TIMEOUT = 20
#: The largest token answer read — a few tokens and their lifetimes, never more.
_MAX_TOKEN_BODY = 64 * 1024
#: The sign-in server's `error` / `error_description`, as far as they are read.
_MAX_ERREUR = 300

# {opaque key: (access_token, epoch expiry)} — never in the database.
_JETONS: dict[str, tuple[str, float]] = {}
_VERROU = threading.Lock()
# {row hash: lock} — one renewal at a time per row in this process.
_RENOUVELLEMENTS: dict[str, threading.Lock] = {}


class ApiOAuthRefused(RuntimeError):
    """A named refusal of this mode, safe to show (never a secret, never a URL with a
    query). `code` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _reconnecter(account: str) -> str:
    return (f"click « Connect » again on the Custom API app « {account} » "
            "(your connectors page)")


# --- the app's sign-in coordinates -------------------------------------------

def redirect() -> str:
    """oto's callback — what an admin registers as redirect URI at the sign-in server."""
    return oauth_flow.redirect_uri(CALLBACK_PATH)


def _coordonnees(conn: dict, app_conn: Optional[dict] = None) -> dict:
    """`authorize_url`, `token_url`, `oauth_client_id`, `scope`, `resource` of an app (or
    of a served key: same names), or `ApiOAuthRefused` naming what is missing or what
    mixes origins. `app_conn` = the app's connection when `conn` is a served key (it
    carries the discovered issuer). For an MCP server, `resource` is ALWAYS its address."""
    from ..connectors import api_apps
    if (conn.get("auth_mode") or "").strip() != MODE:
        raise ApiOAuthRefused("api_app_not_oauth_user",
                              "This Custom API app does not use per-member sign-in "
                              "(`oauth2_user` mode).")
    out = {k: (conn.get(k) or "").strip() for k in
           ("authorize_url", "token_url", "oauth_client_id", "scope", "resource")}
    manquants = [k for k in ("authorize_url", "token_url", "oauth_client_id") if not out[k]]
    if manquants:
        raise ApiOAuthRefused("api_app_incomplete",
                              f"The Custom API app is missing {', '.join(manquants)}: an "
                              "org admin completes its connection.")
    emetteur = (app_conn or conn).get(api_apps.ISSUER)
    refus = api_apps.refus_d_origine({**conn, api_apps.ISSUER: emetteur or ""})
    if refus:
        raise ApiOAuthRefused("api_app_origin_mixed",
                              f"{refus} Nothing was sent: an org admin corrects the app.")
    if api_apps.est_mcp_par_membre(conn):
        out["resource"] = api_apps.ressource_canonique(conn.get("base_url") or "")
    return out


def hotes(app: dict) -> dict:
    """What the member is about to trust, to show before « Connect »: the host of the
    sign-in server their browser goes to, and the host their access is used on."""
    conn = app.get("connection") or {}
    return {"sign_in_host": urlsplit(conn.get("authorize_url") or "").hostname or "",
            "api_host": urlsplit(conn.get("base_url") or "").hostname or ""}


def _scopes(scope: str) -> list[str]:
    return [s for s in (scope or "").split() if s]


# --- starting the flow -------------------------------------------------------

def make_state(sub: str, org_id: int, app_id: int, account: str, verifier: str,
               return_app: str = "") -> str:
    """The signed, single-use state. The PKCE verifier rides SEALED in it (`vs`:
    AES-GCM, bound to this state's `jti`, `flow.seal_code`): the sign-in server and the
    browser carry the state, only this server reads the verifier back."""
    jti = oauth_flow.new_jti()
    return oauth_flow.sign_state(_AUD, {
        "sub": sub, "org": int(org_id), "api_app": int(app_id), "account": account,
        "vs": oauth_flow.seal_code(_AUD, jti, {"v": verifier}), "app": return_app,
        "jti": jti})


def verify_state(state: Optional[str]) -> Optional[dict]:
    """The state's payload if valid, issued FOR this flow, and complete — with the
    unsealed verifier as `v`; else None."""
    data = oauth_flow.read_state(_AUD, state) if state else None
    if not data:
        return None
    ok = (isinstance(data.get("sub"), str) and isinstance(data.get("org"), int)
          and isinstance(data.get("api_app"), int) and isinstance(data.get("account"), str)
          and isinstance(data.get("vs"), str) and data["vs"]
          and isinstance(data.get("jti"), str) and data["jti"])
    if not ok:
        return None
    scelle = oauth_flow.unseal_code(_AUD, data["jti"], data.pop("vs"))
    if not scelle or not isinstance(scelle.get("v"), str) or not scelle["v"]:
        return None
    data["v"] = scelle["v"]
    if not isinstance(data.get("app"), str):
        data["app"] = ""
    return data


def authorize_url(app: dict, sub: str, return_app: str = "") -> str:
    """The sign-in server's URL for `sub` to connect to this app. `offline_access` among
    the scopes asks for a refresh token: OpenID servers (Logto, Auth0…) only issue one
    with `prompt=consent`, sent then."""
    c = _coordonnees(app.get("connection") or {})
    verifier, challenge = pkce.pkce_pair()
    params = {"response_type": "code", "client_id": c["oauth_client_id"],
              "redirect_uri": redirect(),
              "state": make_state(sub, int(app["org_id"]), int(app["id"]), app["account"],
                                  verifier, oauth_flow.resolve_return_app(return_app)),
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if c["scope"]:
        params["scope"] = c["scope"]
        if "offline_access" in _scopes(c["scope"]):
            params["prompt"] = "consent"
    if c["resource"]:
        params["resource"] = c["resource"]
    sep = "&" if urlsplit(c["authorize_url"]).query else "?"
    return f"{c['authorize_url']}{sep}{urlencode(params)}"


# --- the vault ---------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prefixe(ligne: tuple) -> str:
    return hashlib.sha256("|".join(ligne).encode()).hexdigest()


def _cle(ligne: tuple, refresh_token: str) -> str:
    """Cache key: the row and its refresh token, hashed (never a clear-text secret)."""
    return f"{_prefixe(ligne)}:{hashlib.sha256(refresh_token.encode()).hexdigest()}"


def _garder(ligne: tuple, refresh_token: str, access_token: str, expires_in) -> None:
    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = 300   # no lifetime announced: short, the next call renews
    with _VERROU:
        _JETONS[_cle(ligne, refresh_token)] = (access_token, time.time() + ttl)


def _en_cache(ligne: tuple, refresh_token: str) -> Optional[str]:
    with _VERROU:
        cached = _JETONS.get(_cle(ligne, refresh_token))
    return cached[0] if cached and cached[1] > time.time() + 60 else None


def oublier(ligne: tuple) -> None:
    """Drops every cached access token of this row — after the API refused one (401)."""
    p = _prefixe(ligne) + ":"
    with _VERROU:
        for k in [k for k in _JETONS if k.startswith(p)]:
            del _JETONS[k]


@contextmanager
def _verrou_ligne(ligne: tuple) -> Iterator[object]:
    """A transaction holding the renewal lock of ONE vault row (advisory lock in the
    shared base: across processes and instances). Yields the connection."""
    from ..db import _connect

    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("custom-api-refresh|" + "|".join(ligne),))
        yield conn


def _corps_borne(r: requests.Response, host: str) -> bytes:
    corps = bytearray()
    for chunk in r.iter_content(65536):
        corps += chunk
        if len(corps) > _MAX_TOKEN_BODY:
            raise oauth_flow.OAuthExchangeRefused(
                f"The sign-in server ({host}) answered more than "
                f"{_MAX_TOKEN_BODY // 1024} KB: nothing was read.")
    return bytes(corps)


def _session(titulaire: tuple) -> requests.Session:
    """A session pinned by the egress guard (checked at each connection)."""
    return egress_transport.session(connector=CONNECTOR, field="token_url",
                                    titulaire=titulaire)


def _detail_d_erreur(body, status: int) -> str:
    """The sign-in server's `error` and `error_description`, bounded and fenced: its
    words, never ours."""
    if not isinstance(body, dict):
        return f"HTTP {status}"
    detail = " : ".join(str(v).strip() for v in (
        body.get("error") or f"HTTP {status}", body.get("error_description")) if v)
    if len(detail) > _MAX_ERREUR:
        detail = detail[:_MAX_ERREUR] + "…"
    return cloture("sign-in-server-error", detail,
                   origine="Error written by the sign-in server", lecture="a diagnostic")


def _titulaire_du_jeton(app: Optional[dict], ligne: Optional[tuple]) -> tuple:
    """On whose behalf the token URL is called: the app's org when the key belongs to
    an app (its `token_url` is the app's, typed by an admin of that org), else the
    key's own vault row (`egress.titulaire_de`) — none without either."""
    if app and app.get("org_id") is not None:
        return egress.titulaire_de("org", app["org_id"])
    return egress.titulaire_de(*(ligne or (None, None))[:2])


def _token_request(token_url: str, data: dict, *, annoncee: bool,
                   titulaire: tuple) -> dict:
    """POST form to the token endpoint; refusals built by us (never `raise_for_status`,
    whose message carries the URL). The token URL is the final address: a redirect is
    refused, never followed, and the answer read is bounded. `annoncee` = the remote
    MCP server announced this URL (discovery): it is checked with no declared egress
    exception (`egress.AGENT`), at every exchange and every renewal; one the admin
    typed gets the named exceptions that name the key's holder (`titulaire`)."""
    titulaire = egress.AGENT if annoncee else titulaire
    try:
        egress.check_url(token_url, connector=CONNECTOR, field="token_url",
                         titulaire=titulaire)
    except egress.EgressRefused as e:
        raise oauth_flow.OAuthExchangeRefused(str(e)) from None
    host = urlsplit(token_url).hostname or "?"
    try:
        r = _session(titulaire).post(token_url, data=data, timeout=_TIMEOUT,
                                    allow_redirects=False, stream=True,
                                    headers={"Accept": "application/json"})
    except egress.EgressRefused as e:
        raise oauth_flow.OAuthExchangeRefused(str(e)) from None
    except requests.RequestException as e:
        raise oauth_flow.OAuthExchangeRefused(
            f"The sign-in server ({host}) could not be reached ({type(e).__name__}).")
    if 300 <= r.status_code < 400:
        r.close()
        raise oauth_flow.OAuthExchangeRefused(
            f"The sign-in server ({host}) answered the token request with a redirect "
            f"(HTTP {r.status_code}): the app's token URL must be the endpoint's final "
            "address.")
    texte = _corps_borne(r, host).decode("utf-8", errors="replace")
    try:
        body = json.loads(texte)
    except ValueError:
        raise oauth_flow.OAuthExchangeRefused(
            f"Unreadable answer from the sign-in server ({host}, HTTP {r.status_code}).")
    if r.status_code >= 400 or (isinstance(body, dict) and "error" in body):
        detail = _detail_d_erreur(body, r.status_code)
        e = oauth_flow.OAuthExchangeRefused(f"The sign-in server ({host}) refused:{detail}")
        e.status_code, e.body_text = r.status_code, texte  # type: ignore[attr-defined]
        raise e
    if not isinstance(body, dict) or not body.get("access_token"):
        raise oauth_flow.OAuthExchangeRefused(
            f"The sign-in server ({host}) returned no access token.")
    return body


def _annoncee(app: Optional[dict], token_url: str) -> bool:
    """The app's `token_url` came from the remote server (MCP discovery), and is the
    one about to be called."""
    from ..connectors import api_apps
    conn = (app or {}).get("connection") or {}
    return api_apps.est_decouvert(conn, "token_url") and conn.get("token_url") == token_url


def _app_de_la_ligne(row: dict) -> Optional[dict]:
    from ..db import api_apps as store
    app_id = (row.get("meta") or {}).get(store.META_KEY)
    try:
        return store.get(int(app_id)) if app_id is not None else None
    except (TypeError, ValueError):
        return None


def _pack(fields: dict) -> str:
    return credentials_store.pack_secret(CONNECTOR, fields)


def verifier_iss(conn: dict, iss: Optional[str]) -> None:
    """RFC 9207 on the return. With a recorded issuer: `iss` is required when the sign-in
    server announced it sends one, and must equal that issuer whenever present. Without
    one (an app declared by hand), a present `iss` must be on `authorize_url`'s origin.
    Raises `ApiOAuthRefused` (`api_oauth_issuer_mismatch`)."""
    from ..connectors import api_apps
    emetteur = (conn.get(api_apps.ISSUER) or "").strip()
    iss = (iss or "").strip() or None
    refus = ApiOAuthRefused("api_oauth_issuer_mismatch",
                            "The sign-in answer did not come from this app's sign-in "
                            "server: nothing was exchanged. Start the connection again.")
    if emetteur:
        if iss is None:
            if conn.get(api_apps.ISS_ANNONCE) is True:
                raise refus
            return
        if iss.rstrip("/") != emetteur.rstrip("/"):
            raise refus
        return
    if iss is not None and api_apps._origine(iss) != api_apps._origine(
            conn.get("authorize_url") or ""):
        raise refus


def exchange_and_persist(d: dict, code: str, *, iss: Optional[str] = None) -> dict:
    """The code brought back by the browser → tokens → the member's key of the app.
    `iss` = the return's issuer parameter (RFC 9207), checked before anything is sent."""
    from ..connectors import api_apps
    from ..db import api_apps as store

    app = store.vivante(d["org"], d["account"])
    if app is None or int(app["id"]) != d["api_app"]:
        raise oauth_flow.CompletionRefused(
            409, "api_app_removed", "This Custom API app was removed or replaced while "
            "you were signing in. Start the connection again from its card.")
    c = _coordonnees(app.get("connection") or {})
    verifier_iss(app.get("connection") or {}, iss)
    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect(), "client_id": c["oauth_client_id"],
            "code_verifier": d["v"]}
    if c["resource"]:
        data["resource"] = c["resource"]
    # The token URL is the APP's, set by an admin of its org: the named exceptions
    # that name that org apply (`_titulaire_du_jeton`).
    tokens = _token_request(c["token_url"], data, annoncee=_annoncee(app, c["token_url"]),
                            titulaire=_titulaire_du_jeton(app, None))
    rt = tokens.get("refresh_token")
    if not rt:
        raise oauth_flow.CompletionRefused(
            502, "no_refresh_token",
            "The sign-in server issued no refresh token, so the connection would stop "
            "within the hour: nothing was saved. An org admin adds `offline_access` to "
            "the app's scopes (and allows refresh tokens for its client).")
    sub, org = d["sub"], d["org"]
    eid = credentials_store.member_id(org, sub)
    _, given, lien = api_apps.completer_la_pose(credentials_store.MEMBER, eid, CONNECTOR,
                                                d["account"], None, {"refresh_token": rt})
    fields = credentials_store.validate_fields(CONNECTOR, given)
    meta = {**credentials_store.meta_fields(CONNECTOR, fields), **lien,
            "connected_at": _iso(datetime.now(timezone.utc)),
            "scopes": tokens.get("scope") or c["scope"]}
    ligne = (credentials_store.MEMBER, eid, d["account"])
    with _verrou_ligne(ligne) as conn:
        credentials_store.set_credential(credentials_store.MEMBER, eid, CONNECTOR,
                                         _pack(fields), set_by=sub, meta=meta,
                                         account=d["account"], conn=conn)
    _garder(ligne, rt, tokens["access_token"], tokens.get("expires_in"))
    logger.info("custom api: member connected to app #%s (org=%s)", d["api_app"], org)
    return {"account": d["account"], "scope": "member"}


def _lire(ligne: tuple) -> dict:
    row = credentials_store.get_credential_with_meta(ligne[0], ligne[1], CONNECTOR,
                                                     account=ligne[2])
    if not row:
        raise ApiOAuthRefused("api_oauth_reconnect",
                              f"No sign-in any more for this app: {_reconnecter(ligne[2])}.")
    return row


def _verrou_memoire(ligne: tuple) -> threading.Lock:
    with _VERROU:
        return _RENOUVELLEMENTS.setdefault(_prefixe(ligne), threading.Lock())


def _renouveler(ligne: tuple, fields: dict, *, reprise: bool = False) -> str:
    with _verrou_memoire(ligne):
        return _renouveler_seul(ligne, fields, reprise=reprise)


def _renouveler_seul(ligne: tuple, fields: dict, *, reprise: bool) -> str:
    row = _lire(ligne)
    stored = credentials_store.unpack_secret(CONNECTOR, row["secret"])
    rt = stored.get("refresh_token") or ""
    if not rt:
        raise ApiOAuthRefused("api_oauth_reconnect",
                              f"This key holds no sign-in: {_reconnecter(ligne[2])}.")
    if (deja := _en_cache(ligne, rt)):
        return deja   # renewed by the call we waited for
    app = _app_de_la_ligne(row)
    c = _coordonnees(fields, (app or {}).get("connection"))
    data = {"grant_type": "refresh_token", "refresh_token": rt,
            "client_id": c["oauth_client_id"]}
    if c["resource"]:
        data["resource"] = c["resource"]
    refus = None
    # The request goes out with NO transaction open: only the write below locks.
    try:
        tokens = _token_request(c["token_url"], data,
                                annoncee=_annoncee(app, c["token_url"]),
                                titulaire=_titulaire_du_jeton(app, ligne))
    except oauth_flow.OAuthExchangeRefused as e:
        if not oauth_flow.grant_is_dead(getattr(e, "status_code", 0),
                                        getattr(e, "body_text", "")):
            raise ApiOAuthRefused(
                "api_oauth_renewal_refused",
                f"{e} The access could not be renewed; if it persists, an org admin "
                "checks the app's sign-in settings.") from e
        refus = e
    if refus is None:
        nouveau = tokens.get("refresh_token") or rt
        garde = True
        if nouveau != rt:
            with _verrou_ligne(ligne) as conn:
                row = _lire(ligne)
                courant = credentials_store.unpack_secret(CONNECTOR, row["secret"])
                if (courant.get("refresh_token") or "") == rt:
                    credentials_store.set_credential(
                        ligne[0], ligne[1], CONNECTOR,
                        _pack({**courant, "refresh_token": nouveau}),
                        set_by=row.get("set_by"), meta=row.get("meta") or {},
                        account=ligne[2], conn=conn)
                else:
                    # Changed while we asked (a reconnection, another renewal): the
                    # row's token stays; ours serves this call only, never cached.
                    garde = False
        if garde:
            _garder(ligne, nouveau, tokens["access_token"], tokens.get("expires_in"))
        connector_health.record_health(CONNECTOR, ligne, True, None)
        return tokens["access_token"]
    if not reprise:
        now = credentials_store.unpack_secret(CONNECTOR, _lire(ligne)["secret"])
        if now.get("refresh_token") != rt:
            return _renouveler_seul(ligne, fields, reprise=True)
    message = (f"The sign-in server no longer accepts this connection (expired or "
               f"revoked): {_reconnecter(ligne[2])}.")
    connector_health.mark_rejected(ligne[0], ligne[1], CONNECTOR, ligne[2], message)
    raise ApiOAuthRefused("api_oauth_reconnect", message) from refus


def access_token_for(ligne: tuple, fields: dict) -> str:
    """A valid access token for this vault row (`(entity_type, entity_id, account)`) of
    a key in `oauth2_user` mode; `fields` = the SERVED key (its app's connection). Raises
    `ApiOAuthRefused` (reconnect, renewal refused, app incomplete)."""
    rt = (fields.get("refresh_token") or "").strip()
    return (_en_cache(ligne, rt) if rt else None) or _renouveler(ligne, fields)


# --- the return: the callback hands over, the front completes (`auth/flow.py`) ------

def still_allowed(d: dict) -> bool:
    """Re-read ON RETURN: still a member of the org, connector still open there, and a
    personal key still allowed for it."""
    from .. import roles
    from ..connectors import activation, cle_perso

    if not roles.is_org_member(d["sub"], d["org"]):
        return False
    if cle_perso.interdite(d["org"], CONNECTOR):
        return False
    return activation.cran_qui_coupe(CONNECTOR, d["org"], None) is None


def _return_state(state: Optional[str]) -> Optional[oauth_flow.ReturnState]:
    d = verify_state(state)
    if d is None:
        return None
    return oauth_flow.ReturnState(d["sub"], d["org"],
                                  oauth_flow.resolve_return_app(d["app"]), CONNECTOR,
                                  d["jti"], d)


def _code_scelle(query, rs: oauth_flow.ReturnState) -> Optional[str]:
    """What the callback hands the front: the code AND the return's `iss`, sealed for
    this state — so that `iss` reaches the exchange as the sign-in server sent it, and a
    code that did not come through this callback completes nothing."""
    code = query.get("code")
    if not code:
        return None
    iss = query.get("iss")
    return oauth_flow.seal_code(_AUD, rs.jti, {"c": code,
                                              "iss": iss if isinstance(iss, str) else None})


def _finish_return(d: dict, code: str) -> dict:
    recu = oauth_flow.unseal_code(_AUD, d["jti"], code)
    if recu is None or not isinstance(recu.get("c"), str) or not recu["c"]:
        raise oauth_flow.CompletionRefused(
            400, "invalid_state", "This Custom API connection link is invalid or has "
            "expired. Start the connection again.")
    if not still_allowed(d):
        oauth_flow.not_allowed(
            "You are no longer a member of this organization, or the Custom API (or a "
            "personal key for it) is now turned off there.")
    try:
        return exchange_and_persist(d, recu["c"], iss=recu.get("iss"))
    except ApiOAuthRefused as e:
        raise oauth_flow.CompletionRefused(409, e.code, e.message)


RETURN = oauth_flow.OAuthReturn(
    audience=_AUD, callback_path=CALLBACK_PATH, label="Custom API", default_card=CONNECTOR,
    read=_return_state, finish=_finish_return, cards=lambda: (CONNECTOR,),
    code=_code_scelle)
oauth_flow.declare_return(RETURN)

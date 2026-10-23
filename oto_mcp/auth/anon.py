"""Shim OAuth ANONYME pour les endpoints MCP publics (`<slug>.mcp.oto.cx`, ADR 0032).

Les connecteurs custom de claude.ai / Mistral EXIGENT un flux OAuth (discovery + DCR +
authorize + token) même pour un serveur qui ne demande AUCUNE auth : sans route OAuth,
`.well-known/*` + `/register` renvoient 404 → le client échoue (« impossible de
s'inscrire auprès du service de connexion »). Ce shim satisfait leur flux en
**AUTO-APPROUVANT**, sans le moindre écran de login ni compte créé :
- discovery (`.well-known/*`) + DCR (`/register`) → client_id statique ;
- `/authorize` → **302 immédiat** avec un code (aucun login) ;
- `/token` → délivre un token OPAQUE **sans privilège** (l'app anonyme `/mcp` ne le
  vérifie jamais — elle est sans auth : le token n'ouvre rien).

Sécurité : le seul risque réel est l'open-redirect → `redirect_uri` validé STRICTEMENT
(liste fermée d'hôtes, `_redirect_ok` : claude.ai/.com, chatgpt, mistral, local). PKCE (S256) respecté,
code à usage unique + TTL court. Mono-process → store in-memory suffisant. Host-aware :
resource/issuer dérivés du Host de la requête (= le sous-domaine servi), validé sur la
forme d'un endpoint de projet (`subdomain_project.hote_de_projet`), sinon l'adresse
publique de l'instance.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import time
from urllib.parse import unquote, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from ..json_body import InvalidJsonBody, read_json_body

logger = logging.getLogger("oto_mcp.anon_oauth")


def _cors() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
    }


# ⚠️ La SEULE garde du rappel de ce shim : il délivre un code à tout client qui le demande,
# sans login — le seul risque réel est l'open-redirect (vol de code). Liste FERMÉE :
# claude.ai/.com en https (callback MCP), ChatGPT et Mistral à leurs chemins documentés,
# hôtes locaux en http (Claude Code/desktop, port ignoré). Comparaison de host EXACTE après
# parsing : jamais de startswith sur l'URL brute (contournable via `http://localhost.evil.com`).
_ALLOWED_HTTPS_HOSTS = {"claude.ai", "claude.com"}
_ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CALLBACK_PATH = "/api/mcp/auth_callback"


def _extra_https_hosts() -> set[str]:
    extra = (os.environ.get("OTO_MCP_DCR_ALLOWED_REDIRECTS") or "").strip()
    return {h.strip().lower().rstrip(".") for h in extra.split(",") if h.strip()}


# L'autorité d'un rappel : un host (nom ou IPv6 entre crochets) et un port, RIEN d'autre —
# ni `user@`, ni `%`, ni barre oblique inverse.
_AUTORITE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?$")


def _uri_canonique(uri: str):
    """Le découpage de `uri` si Python et un NAVIGATEUR y lisent le même host, sinon None.

    ⚠️ **Cette liste juge ce que `urlsplit` lit ; le navigateur, lui, suit WHATWG**, et les
    deux divergent : `http://evil.example\\@127.0.0.1/cb` est un rappel LOCAL pour Python
    (ce qui précède `@` est une identité) et part chez `evil.example` dans un navigateur (la
    barre oblique inverse y sépare le chemin). Or cette fonction est la SEULE garde du rappel
    de ce shim — aucun serveur d'autorisation ne le revalide derrière. D'où : ASCII imprimable sans
    barre oblique inverse ni fragment, autorité réduite à host[:port], aucun segment `.` ou
    `..` (encodé ou non), que le navigateur résoudrait vers un autre chemin."""
    if not isinstance(uri, str) or not uri or "#" in uri \
            or any(c <= " " or c > "~" or c == "\\" for c in uri):
        return None
    try:
        p = urlsplit(uri)
        p.port  # un port hors bornes lève
    # noqa: SILENT — fail-closed : un redirect_uri douteux est refusé, sans dire pourquoi
    except Exception:
        return None
    if not _AUTORITE.match(p.netloc):
        return None
    if any(unquote(seg) in (".", "..") for seg in p.path.split("/")):
        return None
    return p


_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _prefixe_borne(chemin: str, prefixe: str, *, suffixe_requis: bool = True) -> bool:
    """`chemin` = `prefixe`, suivi d'AU PLUS un segment (`[A-Za-z0-9_-]+`) — jamais un
    préfixe sans borne : `/connector/oauth/x/../../ailleurs` ou `/auth_callbackXYZ` ne
    passent pas. `suffixe_requis` : le segment est obligatoire (identifiant du connecteur)."""
    if chemin == prefixe:
        return not suffixe_requis
    reste = chemin[len(prefixe):] if chemin.startswith(prefixe + "/") else None
    return reste is not None and bool(_SEGMENT.match(reste[1:]))


def _redirect_ok(uri: str) -> bool:
    p = _uri_canonique(uri)
    if p is None:
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme == "https" and host in (_ALLOWED_HTTPS_HOSTS | _extra_https_hosts()) \
            and _prefixe_borne(p.path, _CALLBACK_PATH, suffixe_requis=False):
        return True
    # ChatGPT (connecteurs MCP) : DEUX formes de rappel, toutes deux documentées
    # par OpenAI (developers.openai.com/plugins/build/auth). Ce n'est pas le mode de
    # connexion qui tranche, c'est le serveur d'AUTORISATION : le rappel propre au
    # connecteur `https://chatgpt.com/connector/oauth/<id>` (<id> varie → préfixe de
    # path, pas l'URI exacte) quand l'AS n'annonce pas l'identification d'émetteur
    # RFC 9207 ; le rappel STABLE `connector_platform_oauth_redirect` quand il
    # l'annonce — et aussi pour tout connecteur créé avant l'apparition du premier,
    # qui garde la forme stable. Refuser la seconde suffit à rendre le connecteur
    # ininstallable, sans qu'aucune trace ne survive plus de 19 h.
    if p.scheme == "https" and host == "chatgpt.com" and (
            _prefixe_borne(p.path, "/connector/oauth")
            or p.path == "/connector_platform_oauth_redirect"):
        return True
    # Mistral (Le Chat, connecteurs MCP) : redirect FIXE callback.mistral.ai.
    if p.scheme == "https" and host == "callback.mistral.ai" \
            and _prefixe_borne(p.path, "/v1/integrations_auth"):
        return True
    if p.scheme == "http" and host in _ALLOWED_LOCAL_HOSTS:
        return True
    return False


_ANON_CLIENT_ID = "oto-anon-public"
_SCOPES = ["mcp", "openid", "profile", "email", "offline_access"]

# code -> (code_challenge, redirect_uri, expiry). Mono-process, borné, TTL court.
_CODES: dict[str, tuple] = {}
_CODE_TTL = 300
_CAP = 10_000


def _host_base(request: Request) -> str:
    """La base annoncée par la découverte : l'hôte SERVI, jamais un Host arbitraire.

    ⚠️ Le Host est lu comme `HostDispatch` le lit (dernier en-tête `host` du scope) — c'est
    celui dont il a prouvé le projet publié avant de router ici. Starlette rend le
    PREMIER : deux en-têtes `Host` faisaient annoncer comme issuer et `resource` un hôte
    qu'aucun projet ne sert. Un hôte qui n'a pas la forme d'un endpoint de projet retombe
    sur l'adresse publique de l'instance."""
    from .. import config, subdomain_project
    brut = dict(request.scope.get("headers") or []).get(b"host", b"").decode("latin-1")
    host = subdomain_project.hote_de_projet(brut)
    if host is None:
        logger.warning("anon discovery: hôte hors forme de projet %r, repli canonique",
                       brut[:100])
        return config.public_base_url()
    return f"https://{host}"


def _as_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": _SCOPES,
    }


def _prm(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": _SCOPES,
        "bearer_methods_supported": ["header"],
    }


def _s256(verifier: str) -> str:
    d = hashlib.sha256((verifier or "").encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def _put_code(code: str, challenge: str, redirect_uri: str) -> None:
    _CODES[code] = (challenge, redirect_uri, time.time() + _CODE_TTL)
    while len(_CODES) > _CAP:
        _CODES.pop(next(iter(_CODES)))


def make_routes() -> list[Route]:
    async def prm(request):
        return JSONResponse(_prm(_host_base(request)))

    async def as_meta(request):
        return JSONResponse(_as_metadata(_host_base(request)))

    async def oidc_meta(request):
        base = _host_base(request)
        return JSONResponse({**_as_metadata(base),
                             "subject_types_supported": ["public"],
                             "id_token_signing_alg_values_supported": ["RS256"]})

    async def register(request):
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        # Corps ABSENT ⇒ `{}` : un client qui s'enregistre sans métadonnées garde
        # exactement le comportement d'avant. Corps ILLISIBLE ⇒ 400 nommé — RFC 7591
        # §3.1 attend un document JSON, et deviner à sa place enregistrerait un client
        # dont les `redirect_uris` ne sont pas ceux qu'il a écrits.
        try:
            body = await read_json_body(request)
        except InvalidJsonBody as e:
            return JSONResponse({"error": "invalid_client_metadata",
                                 "error_description": e.detail},
                                status_code=400, headers=_cors())
        return JSONResponse({
            "client_id": _ANON_CLIENT_ID,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none",
            "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": body.get("response_types", ["code"]),
            "client_name": body.get("client_name"),
        }, status_code=201, headers=_cors())

    async def authorize(request):
        p = request.query_params
        redirect_uri = p.get("redirect_uri", "")
        state = p.get("state", "")
        challenge = p.get("code_challenge", "")
        # Anti open-redirect : seul un callback client connu est accepté (aucune donnée
        # sensible en jeu — le token n'ouvre rien — mais on ne devient pas un redirecteur).
        if not _redirect_ok(redirect_uri):
            logger.warning("anon authorize: redirect_uri refusé %r", redirect_uri)
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        code = secrets.token_urlsafe(32)
        _put_code(code, challenge, redirect_uri)  # AUTO-APPROUVE (pas de login)
        sep = "&" if "?" in redirect_uri else "?"
        params = {"code": code}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    async def token(request):
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "authorization_code":
            rec = _CODES.pop(form.get("code", ""), None)
            if not rec or rec[2] < time.time():
                return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_cors())
            challenge, _redirect_uri, _ = rec
            if challenge and _s256(form.get("code_verifier", "")) != challenge:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"},
                                    status_code=400, headers=_cors())
        elif grant != "refresh_token":  # refresh : token sans privilège → ré-émission libre
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=_cors())
        return JSONResponse({
            "access_token": "anon-" + secrets.token_urlsafe(24),
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "anon-" + secrets.token_urlsafe(24),
            "scope": "mcp",
        }, headers=_cors())

    return [
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", as_meta, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", as_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration", oidc_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration/mcp", oidc_meta, methods=["GET"]),
        Route("/register", register, methods=["POST", "OPTIONS"]),
        Route("/oauth/register", register, methods=["POST", "OPTIONS"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/token", token, methods=["POST", "OPTIONS"]),
    ]

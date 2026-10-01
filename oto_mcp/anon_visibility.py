"""La FACE ANONYME du registre MCP — et sa visibilité, allowlist FIGÉE (ADR 0032, amende #44).

Le registre MCP se construit UNE fois (`server._build_mcp`, oto-backend#534). Le mode non
authentifié n'est pas un second serveur : c'est une **face** du même registre, montée
SANS auth (`face_anonyme`), servie par `subdomain_project.HostDispatch` pour un projet
publié `anonymous` ou `secret` (`<slug>.mcp.<D>`, `<slug>.share.<D>`).

Ce qu'elle laisse voir est déclaré ICI et nulle part ailleurs : sur la face anonyme, la
visibilité n'est PAS la denylist per-(sub, org) habituelle (il n'y a pas de sub) — c'est
l'**allowlist** du projet résolu (`subdomain_project.current_allowlist`, preset + opt-ins).
Tout le reste est masqué **fail-CLOSED** : une face anonyme atteinte sans projet résolu
ne voit RIEN. Hors de la face anonyme, le middleware ne fait rien — il est dans la chaîne
commune, il ne restreint que la face qui le demande. Banc :
`tests/test_face_anonyme_un_registre.py`.
"""
from __future__ import annotations

import contextvars
import logging

import fastmcp
from fastmcp.server.http import create_streamable_http_app
from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import subdomain_project

logger = logging.getLogger(__name__)

# Backstop FAIL-CLOSED : tous les noms de tools vus lors d'un list_tools réussi. Si le
# listing échoue (rare, in-memory), on masque au moins ce qu'on connaît (sinon la
# denylist serait incomplète → fuite publique).
_ALL_NAMES_CACHE: set[str] = set()

# Posée par la face anonyme pour la durée de chaque requête HTTP qu'elle reçoit. La
# session MCP naît de l'`initialize` et hérite de son contexte : c'est le même chemin
# que fastmcp emprunte pour rendre la requête HTTP visible à `on_initialize`.
_FACE_ANONYME: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oto_face_anonyme", default=False)


class _MarqueFaceAnonyme:
    """ASGI pur : marque toute requête entrée par la face anonyme."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        tok = _FACE_ANONYME.set(True)
        try:
            return await self.app(scope, receive, send)
        finally:
            _FACE_ANONYME.reset(tok)


def face_anonyme(mcp: fastmcp.FastMCP):
    """L'app HTTP SANS auth du registre `mcp` — la face qu'un projet publié sert à qui
    n'a pas de compte. Même serveur que la face authentifiée (`mcp.http_app()`) : aucun
    second `register_all`. Les réglages de transport sont ceux que `http_app` lit, seul
    `auth` diffère (fastmcp n'offre pas de face sans auth d'un serveur qui en porte une).

    Le shim OAuth anonyme (ADR 0032) y est monté : claude.ai/Mistral exigent un flux
    OAuth pour un connecteur custom, même sans auth — sans ces routes, DCR 404 =
    « impossible de s'inscrire ». Inséré avant le catch /mcp de FastMCP."""
    from .auth import anon as anon_oauth
    s = fastmcp.settings
    app = create_streamable_http_app(
        server=mcp, streamable_http_path=s.streamable_http_path, auth=None,
        json_response=s.json_response, stateless_http=s.stateless_http, debug=s.debug,
        host_origin_protection=s.http_host_origin_protection,
        allowed_hosts=s.http_allowed_hosts, allowed_origins=s.http_allowed_origins)
    for route in reversed(anon_oauth.make_routes()):
        app.router.routes.insert(0, route)
    app.add_middleware(_MarqueFaceAnonyme)
    return app


class AnonymousVisibilityMiddleware(Middleware):
    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        anon_ctx = subdomain_project.current_anon_context()
        if not _FACE_ANONYME.get() and anon_ctx is None:
            return result           # face authentifiée : la visibilité est celle du sub
        ctx = getattr(context, "fastmcp_context", None)
        if ctx is None:
            logger.warning("anon visibility: fastmcp_context is None")
            return result
        # Face anonyme sans projet résolu → allowlist VIDE : rien n'est servi.
        allow = subdomain_project.current_allowlist() or frozenset()
        try:
            all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
            all_names = {t.name for t in all_tools}
            _ALL_NAMES_CACHE.update(all_names)
        except Exception as e:  # noqa: BLE001
            logger.warning("anon visibility: list_tools failed (fail-closed via cache): %s", e)
            all_names = set(_ALL_NAMES_CACHE)
        to_hide = all_names - set(allow)
        if to_hide:
            await disable_components(ctx, names=to_hide, components={"tool"})
        return result

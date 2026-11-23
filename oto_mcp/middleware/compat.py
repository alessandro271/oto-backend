"""`CompatMiddleware` — le tampon de compatibilité (`deprecations`) au bord MCP.

Une ancienne forme d'argument, retirée du code métier, est traduite ICI vers la forme
d'aujourd'hui quand l'instance a allumé l'entrée qui la couvre — avant que l'outil ne
la lise, après que le contexte de l'appel (org, run, relevé du journal) est posé
(`CallContextMiddleware`, plus haut), et SOUS le journal des appels
(`calllog.ToolCallLogger`) : un appel traduit comme un appel refusé y laisse sa ligne
et sa trace (`args.ref_kind`).

Une seule entrée aujourd'hui : le tableau désigné par son NOM
(`deprecations.traduire_arguments`). L'appel relayé (`oto_call`, recettes) passe par
la même fonction, dans `tools/meta.executer_cible`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp.server.middleware import Middleware
from mcp.types import INVALID_PARAMS, ErrorData
from starlette.concurrency import run_in_threadpool

from .. import deprecations
from ..auth.hooks import current_user_sub_from_token
from ..mcp_errors import McpError


async def traduire(name: str, args: dict, sub: Optional[str]) -> None:
    """Traduit `args` pour l'outil `name` (hors de la boucle s'il y a à faire) ; un nom
    de tableau qui ne désigne rien est refusé comme introuvable, sans appeler l'outil."""
    if not deprecations.a_traduire(name, args):
        return
    try:
        await run_in_threadpool(deprecations.traduire_arguments, name, args, sub)
    except deprecations.TableauIntrouvable as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e),
                                 data={"oto": {"code": "datastore_not_found",
                                               "retryable": False}})) from None


class CompatMiddleware(Middleware):
    """Traduit les anciennes formes d'arguments d'un appel d'outil (`deprecations`)."""

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "") or ""
        args = getattr(context.message, "arguments", None)
        if isinstance(args, dict):
            await traduire(name, args, current_user_sub_from_token())
        return await call_next(context)

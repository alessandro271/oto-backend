"""Une erreur non gérée sous `/api/*` répond en JSON, AVEC ses en-têtes CORS.

**Le défaut qu'il ferme.** Les en-têtes CORS sont posés par les fabriques de réponse
(`json_response`, `json_error`, `_file` — `api/base.py`), pas par un middleware. Une
exception qu'aucun handler ne rattrape remonte donc jusqu'au `ServerErrorMiddleware`
de Starlette, qui répond un 500 `text/plain` NU : le navigateur, faute
d'`Access-Control-Allow-Origin`, refuse de la lire et la rapporte en « Failed to
fetch ». Le front n'a alors aucun moyen de distinguer une panne du serveur d'une
coupure réseau — vécu le 01/10/2026 : une sonde en 500 juste après une pose réussie,
l'utilisateur a cru que l'ajout avait échoué et a recliqué.

**Ce qu'il fait, et ce qu'il ne fait pas.** Si la réponse n'a pas encore commencé,
il envoie `500 {"error": "internal_error"}` avec les en-têtes CORS de l'origine de la
requête — aucun détail, aucune trace : rien de l'intérieur ne sort. Puis il RELÈVE
l'exception : le `ServerErrorMiddleware` voit une réponse déjà partie et n'en écrit pas
d'autre, mais le traceback reste journalisé (uvicorn) et capté par Sentry comme avant.
On rend l'erreur lisible au navigateur ; on ne l'avale jamais.

Monté au plus PRÈS des routes (premier `add_middleware`), pour que le journal REST
(`RestCallLogger`, plus extérieur) voie le statut 500 réel. Pass-through total hors
`/api/*` : n'altère pas le streaming `/mcp`.
"""
from __future__ import annotations

import json

from .base import _cors_headers

_CORPS = json.dumps({"error": "internal_error"}).encode()


class ErreurInterneAvecCors:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        demarre = False

        async def _send(message):
            nonlocal demarre
            if message.get("type") == "http.response.start":
                demarre = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            if not demarre:
                origine = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                                if k == b"origin"), None)
                entetes = [(b"content-type", b"application/json"),
                           (b"content-length", str(len(_CORPS)).encode())]
                entetes += [(k.lower().encode(), v.encode())
                            for k, v in _cors_headers(origine).items()]
                await send({"type": "http.response.start", "status": 500,
                            "headers": entetes})
                await send({"type": "http.response.body", "body": _CORPS})
            raise

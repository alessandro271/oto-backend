"""Les retours de consentement des connecteurs — une route par fournisseur, un seul code.

Le fournisseur redirige le NAVIGATEUR vers `…/oauth/callback` (`…/connect/callback` pour
WordPress) : pas d'en-tête d'auth, et la réponse est une 302 vers le front. C'est hors du
contrat capacité (JSON + autz), d'où ces routes écrites à la main, classées par NATURE
dans `tests/test_rest_modules_are_capabilities.py`.

Le callback ne range rien : il renvoie le front sur `connect=confirm`, code et state dans
le fragment, et le front termine avec son jeton (`POST /api/me/connectors/{name}/complete`,
`capabilities/connectors/complete.py`). Toute la mécanique vit dans
`auth/flow.py` (`callback_url`, `complete`) ; chaque fournisseur la déclare depuis son
module (`oauth_flow.declare_return`). Les CHEMINS sont ceux enregistrés chez les
fournisseurs, octet pour octet : ils ne bougent pas.
"""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ..auth import flow as oauth_flow


def _endpoint(retour: oauth_flow.OAuthReturn):
    async def callback(request: Request) -> Response:
        # Lecture du state, et pour un `side_return` consommation d'un jti : SQL
        # synchrone, hors de la boucle (oto-backend#867).
        url = await run_in_threadpool(oauth_flow.callback_url, retour,
                                      request.query_params)
        return RedirectResponse(url=url, status_code=302)
    return callback


def make_routes() -> list[Route]:
    # Triées par chemin : l'ordre ne dépend pas de l'ordre d'import des modules.
    return [Route(r.callback_path, _endpoint(r), methods=["GET"])
            for r in sorted(oauth_flow.load_returns().values(),
                            key=lambda r: r.callback_path)]

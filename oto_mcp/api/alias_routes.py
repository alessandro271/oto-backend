"""Les anciens chemins REST, montés en **308** vers ceux d'aujourd'hui.

Un chemin servi ne se renomme pas : il se DOUBLE. Le nouveau chemin est la vraie
route (dérivée d'une capacité, décrite dans `/openapi.json`, avec son autz) ;
l'ancien est monté ici, ne fait rien d'autre que rediriger, et **reste servi tant que
son usage compté n'est pas nul** (décision du 23/11/2026) :
chaque redirection laisse une ligne dans `tool_calls`, et la 308 porte
`Deprecation: true` sans `Sunset` — aucune date de retrait n'est servie.

**Pourquoi 308 et pas 301/302.** Un 301/302 autorise le client à retomber en GET —
un `POST …/publish` deviendrait un `GET` sur la nouvelle route, donc un 405 ou pire,
un no-op silencieux. Le 308 conserve la méthode ET le corps : c'est le seul code qui
dit « même requête, autre adresse ».

**Trois crans qui ne se voient qu'en les nommant :**

- **La query string est reportée.** Le build de la vitrine appelle
  `…/library?limit=200` ; un 308 qui la perdrait rendrait 100 entrées au lieu de 200,
  sans qu'aucun code d'erreur ne le signale.
- **Les en-têtes CORS sont posés sur la redirection elle-même.** Un navigateur
  vérifie CORS sur CHAQUE réponse d'une chaîne de redirections : une 308 nue ferait
  échouer le `fetch` cross-origin de la vitrine, et l'erreur ne nommerait pas la
  redirection. Le préflight `OPTIONS`, lui, reste servi par le handler partagé — il
  n'est jamais redirigé, sinon le navigateur abandonnerait avant d'essayer.
- **Ces routes sont montées EN DERNIER** (cf. `routes.make_routes`). Un alias ne peut
  alors capturer que ce que rien d'autre ne sert : il ne peut pas éclipser une vraie
  route par un placeholder trop gourmand.

**Le compteur.** La redirection n'authentifie pas et n'écrit rien elle-même : elle
dépose dans le scope ce que le journal des routes (`routes.RestCallLogger`, chemin
d'écriture asynchrone habituel, `kind='rest'`) doit écrire — `tool` = verbe + chemin
d'aujourd'hui, `args.deprecated_name` = l'ancien chemin GABARIT (jamais le chemin
réel, qui peut porter un identifiant). L'org, le compte et le jeton y sont ceux que le
journal connaît déjà ; sans authentification, la ligne porte ce qui est connu.

Ces chemins sont classés `NATURE` dans `tests/test_rest_modules_are_capabilities.py`
et non `DEBT` : ils ne portent aucun métier à migrer, ils portent un compteur.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from .. import deprecations
from .base import CLE_ALIAS_DEPRECIE, _cors_headers


def _redirection(alias: deprecations.AliasRest):
    async def _handler(request: Request) -> Response:
        url = deprecations.cible(alias, dict(request.path_params),
                                 request.url.query)
        r = RedirectResponse(url, status_code=308)
        for k, v in _cors_headers(request.headers.get("origin")).items():
            r.headers[k] = v
        # Un intégrateur qui lit ses logs voit la dépréciation sans ouvrir la doc. Pas
        # de `Sunset` : aucune date de retrait n'est servie (décision du 23/11/2026).
        r.headers["Deprecation"] = "true"
        request.scope[CLE_ALIAS_DEPRECIE] = {
            "tool": f"{alias.verbe} {alias.nouveau}",
            "deprecated_name": alias.ancien,
        }
        return r

    _handler.__name__ = "alias_deprecie"
    return _handler


def make_routes(options_handler) -> list:
    """Une route de redirection (+ son préflight) par alias dérivé.

    L'ORDRE suit `deprecations.alias_rest_derives`, qui le tient : un chemin littéral
    doit précéder un chemin à placeholder qui l'engloberait.
    """
    routes: list = []
    # ⚠️ Les alias des renommages `namespace` → `datastore` et `http` → `api` sont
    # DÉRIVÉS des routes montées, pas listés (`deprecations.alias_rest_derives`). Ils
    # viennent EN PREMIER dans cette boucle mais restent, comme tout ce module, montés
    # en dernier par `routes.make_routes` : un alias ne peut donc éclipser aucune vraie
    # route.
    for alias in deprecations.alias_rest_derives():
        routes.append(Route(alias.ancien, _redirection(alias), methods=[alias.verbe]))
        routes.append(Route(alias.ancien, options_handler, methods=["OPTIONS"]))
    return routes

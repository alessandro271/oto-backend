"""La porte HTTP de la face MCP : la lecture d'entrée du compte, faite UNE fois, et le
401 d'un jeton émis avant la fermeture des sessions de son compte.

**Pourquoi au niveau HTTP.** Un jeton émis avant la fermeture (`session_revoquee`) n'est
plus une identité : le client doit relancer son OAuth tout de suite. Seul un **401 HTTP**
avec `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750 §3.1) le lui dit ; une
erreur JSON-RPC (`-32003`) dans une réponse 200 le laissait réessayer le même jeton
jusqu'à son terme. Or le middleware de garde (`AccountSuspendedMiddleware`) tourne APRÈS
la couche HTTP : il ne peut plus changer le statut.

**Où.** Dans le backend d'authentification que fastmcp pose devant toute l'app
(`AuthenticationMiddleware` + `BearerAuthBackend`, via `get_middleware` du fournisseur) :
c'est le point où le jeton vient d'être vérifié et où la requête est encore HTTP. Le
refus lève `SessionRevoquee` ; l'`on_error` du middleware rend le 401, avec le même
`resource_metadata` que les autres 401 de `/mcp` (celui que `RequireAuthMiddleware`
reçoit de `http_app`). Les middlewares ASGI de `main` (hôte de tenant, texte d'accueil)
le voient passer comme les autres.

**Toute méthode sur `/mcp`.** La lecture d'entrée (`garde_identite.entree_du_jeton` →
une `db.get_suspension` : pause ET instant « révoqué avant ») se fait ICI pour toute
requête authentifiée sur `/mcp` : `POST` (les messages JSON-RPC), mais aussi `GET` (le
flux de notifications, qui sans cela resterait ouvert sous un jeton révoqué) et `DELETE`
(fin de session). Son résultat est posé dans le `scope` de la requête (`CLE`) ; la garde
d'identité, qui ne voit que les messages d'un `POST`, le reprend (`entree_lue`) au lieu de
relire : sur un `POST`, la lecture a changé de place, pas de nombre ; un `GET`/`DELETE`
en paie une. La face REST, elle, ne la paie pas ici — le backend est posé sur toute
l'app, mais `api.base` a sa propre porte et sa propre lecture.

**Ce qui reste à la garde.** La pause et le tenant désactivé restent des refus du
protocole (`-32003`), servis par `AccountSuspendedMiddleware` : leur forme servie ne
change pas. La garde rejuge aussi la révocation sur l'état repris (sans relire) : un
filet pour les chemins sans porte HTTP, jamais un second refus sur le chemin nominal.

**Sub lu.** Le sub du jeton tel que vérifié, AVANT le drain d'alias : c'est celui du
compte Logto dont `fermer_tout` a fermé les sessions. Si le drain le canonicalise en un
autre sub, la garde ne reprend pas cet état (il ne vaut pas pour le sub canonique) et lit
le sien : une lecture de plus pour un ancien identifiant redirigé, et seulement pour lui.
La face REST juge de même (`api.base._authenticate`, même `entree_du_jeton`).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp.server.auth import RemoteAuthProvider
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.routes import build_resource_metadata_url
from starlette.authentication import AuthenticationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from .. import account_suspension, garde_identite, session_revoquee

logger = logging.getLogger(__name__)

#: Les chemins du transport MCP authentifié (celui de `http_app()`, par défaut).
CHEMINS = frozenset({"/mcp", "/mcp/"})

#: La clé du `scope` où la lecture d'entrée attend la garde : `(sub, état lu)`.
CLE = "oto.entree_mcp"

#: Le `error_description` du challenge. ASCII seul : c'est la grammaire de RFC 6750 §3
#: (pas de guillemet, pas d'antislash, rien hors de %x20-7E). Le message en français
#: voyage dans le corps (`detail`).
DESCRIPTION = ("This token was issued before the account's sessions were closed. "
               "Sign in again to get a new token.")


class SessionRevoquee(AuthenticationError):
    """Le jeton présenté a été émis avant l'instant « révoqué avant » de son compte."""


class BackendOto(BearerAuthBackend):
    """Le backend de fastmcp, plus la lecture d'entrée du compte sur toute requête
    `/mcp` (toute méthode : le flux `GET` et le `DELETE` sont jugés comme le `POST`)."""

    async def authenticate(self, conn: HTTPConnection):
        admis = await super().authenticate(conn)
        if admis is None or conn.scope.get("path") not in CHEMINS:
            return admis
        jeton = admis[1].access_token
        sub = (getattr(jeton, "claims", None) or {}).get("sub")
        if not sub:
            return admis
        # Une panne de lecture REMONTE (fail-closed), comme dans la garde.
        lue, revoque = await run_in_threadpool(
            garde_identite.entree_du_jeton, sub, session_revoquee.emis_le_du_jeton(jeton))
        conn.scope[CLE] = (sub, lue)
        if revoque:
            raise SessionRevoquee(revoque[0])
        return admis


def challenge(resource_metadata_url) -> str:
    """L'en-tête `WWW-Authenticate` du refus, à la forme de ceux de `RequireAuthMiddleware`."""
    parties = ['error="invalid_token"', f'error_description="{DESCRIPTION}"']
    if resource_metadata_url:
        parties.append(f'resource_metadata="{resource_metadata_url}"')
    return "Bearer " + ", ".join(parties)


def reponse_401(resource_metadata_url) -> Response:
    """Le 401 servi : le challenge, et un corps JSON qui nomme la cause (`code`)."""
    return JSONResponse(
        {"error": "invalid_token", "error_description": DESCRIPTION,
         "code": session_revoquee.CODE, "detail": session_revoquee.MESSAGE},
        status_code=401,
        headers={"WWW-Authenticate": challenge(resource_metadata_url)})


class FournisseurOto(RemoteAuthProvider):
    """`RemoteAuthProvider` dont le backend d'authentification est `BackendOto`.

    On reprend la liste de fastmcp et on n'y remplace que le backend et l'`on_error` du
    middleware d'authentification : ce qu'amont y ajoute un jour reste posé."""

    def get_middleware(self) -> list:
        import fastmcp
        url = self._get_resource_url(fastmcp.settings.streamable_http_path)
        metadata = build_resource_metadata_url(url) if url else None

        def _sur_refus(conn: HTTPConnection, exc: Exception) -> Response:
            if isinstance(exc, SessionRevoquee):
                return reponse_401(metadata)
            return AuthenticationMiddleware.default_on_error(conn, exc)

        poses = []
        for m in super().get_middleware():
            if m.cls is AuthenticationMiddleware:
                m = Middleware(AuthenticationMiddleware, backend=BackendOto(self),
                               on_error=_sur_refus)
            poses.append(m)
        return poses


def entree_lue(sub: Optional[str]):
    """L'état d'entrée lu par `BackendOto` pour CE sub dans la requête MCP en cours, ou
    `account_suspension.A_LIRE` s'il n'y en a pas (autre sub après le drain d'alias,
    hors requête HTTP) : la garde lit alors elle-même. Lecture de contexte, aucune base."""
    try:
        from fastmcp.server.dependencies import get_http_request
        lu = get_http_request().scope.get(CLE)
    # noqa: SILENT — hors requête HTTP (transport en mémoire, travail de fond) : la garde lit
    except RuntimeError:
        return account_suspension.A_LIRE
    if not lu:
        return account_suspension.A_LIRE
    return garde_identite.reprise(lu[1], lu[0], sub)

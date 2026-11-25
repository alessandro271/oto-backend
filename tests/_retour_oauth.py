"""Le retour d'un consentement, de bout en bout, pour les tests des connecteurs.

`rappel` joue le VRAI callback (la route de `api/oauth_returns.py`) et rend l'URL où il
renvoie le navigateur. `terminer` enchaîne comme un front : callback, puis, si le
callback a remis `connect=confirm`, la complétion commune (`auth/flow.complete`) avec le
jeton de `sub` (par défaut, la personne du state). Il rend l'issue :

- le mot du callback quand il n'a rien remis (`error`, `denied`, `admin_required`…) ;
- `connected` quand la complétion a rangé la connexion ;
- le code du refus nommé sinon (`not_allowed`, `state_used`, `exchange_failed`…).

Sans base, la table des `jti` consommés est un ensemble en mémoire, sauf si le test l'a
lui-même remplacée.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from typing import Optional

from starlette.requests import Request

from oto_mcp.auth import flow as oauth_flow


#: Les `jti` consommés quand la base n'est pas là (un `jti` est unique : l'ensemble peut
#: survivre d'un test à l'autre sans fausser un rejeu).
_CONSOMMES: set = set()


def _consommer_en_memoire(audience: str, jti: str) -> bool:
    if (audience, jti) in _CONSOMMES:
        return False
    _CONSOMMES.add((audience, jti))
    return True


def _sans_base():
    """Remplace la table des `jti` par un ensemble en mémoire, sauf si le test l'a déjà
    remplacée (ou a remplacé `consume_state` lui-même)."""
    from oto_mcp import db
    vraie = db.consume_state_jti
    if getattr(vraie, "__module__", "").startswith("oto_mcp.db"):
        db.consume_state_jti = _consommer_en_memoire
        return lambda: setattr(db, "consume_state_jti", vraie)
    return lambda: None


def _route(chemin: str):
    from oto_mcp.api import oauth_returns
    return next(r for r in oauth_returns.make_routes() if r.path == chemin)


def rappel(chemin: str, query: dict) -> str:
    """L'URL où le callback de `chemin` renvoie le navigateur."""
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    req = Request({"type": "http", "method": "GET", "path": chemin, "headers": [],
                   "query_string": urllib.parse.urlencode(query).encode(),
                   "path_params": {}}, receive=_receive)
    remettre = _sans_base()
    try:
        return asyncio.run(_route(chemin).endpoint(req)).headers["location"]
    finally:
        remettre()


def connect(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["connect"][0]


def terminer(chemin: str, query: dict, *, sub: Optional[str] = None) -> tuple[str, dict]:
    """`(issue, détail)` : `détail` = la réponse 200 de la complétion, ou
    `{"details", "message"}` du refus, ou `{"location"}` du callback."""
    url = rappel(chemin, query)
    if connect(url) != "confirm":
        return connect(url), {"location": url}
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    remis = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment)
    code, state = remis["code"][0], remis["state"][0]
    retour = next(r for r in oauth_flow.load_returns().values() if r.callback_path == chemin)
    if sub is None:
        sub = retour.read(state).sub
    remettre = _sans_base()
    try:
        out = oauth_flow.complete(retour, sub, code, state, card=q["connector"][0])
        return "connected", out
    except oauth_flow.CompletionRefused as e:
        return e.code, {"details": e.details, "message": e.message}
    finally:
        remettre()

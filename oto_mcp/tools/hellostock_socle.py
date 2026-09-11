"""Socle partagé des modules du connecteur `hellostock` (lectures, écritures).

Le connecteur tient sur deux modules (`Connector.modules` au registre) : les
lectures dans `tools/hellostock.py`, les trois gestes qui agissent sur la
marketplace de production dans `tools/hellostock_ecritures.py`. Ce fichier porte
ce qu'ils ont en commun — la résolution du jeton, la traduction d'un refus de
HelloStock en consigne, la projection d'une page — pour qu'un correctif ne
couvre jamais la moitié du connecteur. Il n'a pas de `register()` : ce n'est pas
un connecteur, c'est un helper.

**Les deux refus d'authentification ne se ressemblent pas, et ne se soignent pas
pareil.** 401 : le jeton est inconnu ou révoqué — on en recrée un. 403 : le jeton
est bon mais son compte n'est pas administrateur (le rôle est relu à chaque appel,
donc un compte rétrogradé passe de 200 à 403 sans que le jeton change) — recréer
un jeton n'y fera rien. Les confondre renverrait l'utilisateur au mauvais geste.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterable

from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError
from .. import access, output_projection

if TYPE_CHECKING:  # l'annotation de `_client()` seulement — jamais évaluée
    from oto.tools.hellostock import HelloStockAdminClient

# Où l'utilisateur crée son jeton, dans SON compte HelloStock.
OU_CREER_LE_JETON = "hellostock.fr → Mon espace → Réglages → « Jetons d'API »"


def _client() -> HelloStockAdminClient:
    """Le client HelloStock pour le jeton de CET appelant (byo_user seul : le jeton
    est personnel, il porte les droits de son titulaire).

    L'import réel est fait dans le corps : les tests remplacent le client, et la
    sonde de version-skew lit l'annotation de retour pour vérifier que les méthodes
    appelées par les deux modules existent dans l'oto-core épinglé.
    """
    from oto.tools.hellostock import HelloStockAdminClient

    key, _is_platform = access.resolve_api_key("hellostock")
    return HelloStockAdminClient(token=key)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _carte() -> str:
    """Où l'on REMPLACE le jeton côté oto, pour CE compte (l'adresse suit le tenant
    de l'appelant, jamais une adresse en dur)."""
    from .. import config
    from ..auth.hooks import current_user_sub_from_token
    return f"{config.dashboard_url_for(current_user_sub_from_token())}/account"


def refus(e: Any, *, carte: bool = True) -> str:
    """La consigne qui correspond à un refus de HelloStock (`UpstreamHTTPError`)."""
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("error") or (e.body if isinstance(e.body, str) else "")
    ou = f" — carte HelloStock de ton compte oto : {_carte()}" if carte else ""
    if status == 401:
        return ("HelloStock refuse ce jeton (401) : il est inconnu ou révoqué. Crée un "
                f"nouveau jeton sur {OU_CREER_LE_JETON} (il n'est affiché qu'une fois), "
                f"puis remplace l'ancien{ou}.")
    if status == 403:
        return ("HelloStock reconnaît ce jeton, mais son compte n'est pas administrateur "
                "de la marketplace (403) : cette API leur est réservée, et recréer un "
                "jeton n'y changera rien. Il faut que ce compte reçoive le rôle "
                "d'administrateur dans HelloStock, ou poser le jeton d'un compte qui "
                f"l'a{ou}.")
    if status == 404:
        return f"HelloStock : {detail or 'enregistrement introuvable'} (404) — vérifie l'identifiant."
    return f"HelloStock a refusé la requête (HTTP {status}) : {detail or e.body}"


def traduire(e: Any) -> Exception:
    """L'exception à lever pour un refus de HelloStock (`UpstreamHTTPError`).

    4xx → refus nommé (l'appel est à changer, ou le jeton). 429 et 5xx restent ce
    qu'ils sont : la taxonomie d'erreurs les classe réessayables, à raison."""
    if 400 <= e.status_code < 500 and e.status_code != 429:
        return _bad(refus(e))
    return e


def _run(fn: Callable[[], Any]) -> Any:
    """Exécute un appel au client et traduit ce qui revient en consigne.

    Une `HelloStockProtocolError` (redirection, corps non JSON) n'est PAS traduite :
    c'est un défaut de configuration de notre côté, qui doit se voir tel quel.
    """
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e)) from None
    except UpstreamHTTPError as e:
        raise traduire(e) from None


def _hors_op(op: str, **donnes: Any) -> None:
    """Refuse un argument qui ne s'applique pas à l'`op` choisie, plutôt que de
    l'ignorer : un filtre passé à `op="get"` laisserait croire qu'il a filtré."""
    en_trop = sorted(k for k, v in donnes.items() if v not in (None, False))
    if en_trop:
        raise _bad(f"op='{op}' ne prend pas {en_trop}.")


def projeter(page: Any, drop: Iterable[str], full: bool) -> Any:
    """Page `{items, nextCursor, total}` → mêmes clés, chaque élément sans les
    colonnes `drop`, et un bloc `projection` qui NOMME ce qui a été retiré.
    `full=True` rend la page telle que HelloStock l'a servie. Jamais de coupe dans
    un texte : on retire des colonnes entières, et on le dit."""
    drop = tuple(drop)
    if full or not drop or not isinstance(page, dict):
        return page
    out = output_projection.project(page, items_path="items", item_drop=drop)
    out["projection"] = {"omitted": list(drop),
                         "hint": "full=True rend les enregistrements entiers"}
    return out

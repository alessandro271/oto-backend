"""Nextmotion — le client, les gardes d'arguments, la traduction des erreurs amont et
la sonde, partagés par tous les modules d'outils du connecteur.

Séparés des modules d'outils pour tenir sous 500 lignes. Trois règles vivent ici :
- **aucun argument n'est retenu au silence** : « fourni » se lit `is not None`, jamais
  la vérité — `dry_run=False` et `offset=0` sont des valeurs fournies ;
- **une erreur amont se classe sur `status_code`**, jamais sur le texte du message ;
- **une écriture est un aperçu tant qu'on ne dit pas le contraire** : `dry_run` vaut
  True par défaut (`_serve_write`), l'aperçu valide `data` contre la liste blanche
  d'entrée (`nextmotion_entrees`), relit l'objet visé et dit ce qui partirait, sans
  appeler aucune méthode d'écriture. `Kind`, `Write` et ce qui les sert
  (`_serve_kind`, `_serve`, `_serve_write`, `_check_body`) sont en fin de module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

if TYPE_CHECKING:
    from oto.tools.nextmotion import NextmotionClient

_NAME = "nextmotion"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _client() -> NextmotionClient:
    """Le client pour la clé de CET appelant. L'import réel est dans le corps : les
    tests remplacent la classe du package."""
    from oto.tools.nextmotion import NextmotionClient

    key, _ = access.resolve_api_key(_NAME)
    if not isinstance(key, str) or not key.strip():
        # Vide, le client irait chercher une clé dans l'environnement du serveur.
        raise _bad("Nextmotion : aucune clé API posée pour ce connecteur.")
    return NextmotionClient(api_key=key.strip())


def _run(fn: Callable[[], Any]) -> Any:
    """Exécute un appel au client ; une erreur de validation ou amont devient une
    consigne `INVALID_PARAMS`."""
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e)) from None
    except UpstreamHTTPError as e:
        raise _bad(_upstream_message(e)) from None


def _need(op: str, **required: Any) -> None:
    missing = [n for n, v in required.items() if v is None or v == ""]
    if missing:
        raise _bad(f"op={op!r} exige {', '.join('`' + m + '`' for m in missing)}.")


def _refuse_ignored(op: str, **provided: Any) -> None:
    """Un argument fourni que CET op n'utilise pas est une erreur d'intention.
    `is not None`, jamais la vérité : `False` et `0` sont des valeurs fournies."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} n'utilise pas `{name}`.")


def _paging(limit: Optional[int], offset: Optional[int]) -> dict:
    return {"limit": 50 if limit is None else limit, "offset": 0 if offset is None else offset}


def _error_codes(body: Any) -> set:
    if not isinstance(body, dict):
        return set()
    return {e.get("code") for e in body.get("errors") or [] if isinstance(e, dict)}


def _upstream_message(e: Any) -> str:
    status = e.status_code
    if status == 401:
        return "Nextmotion : clé API refusée (HTTP 401) — invalide, régénérée ou supprimée."
    if status == 403:
        if "non_employee_access_denied" in _error_codes(e.body):
            return ("Nextmotion : accès refusé (HTTP 403) — l'utilisateur de la clé "
                    "n'est pas employé de cette clinique.")
        return "Nextmotion : accès refusé (HTTP 403)."
    if status == 404:
        return "Nextmotion : ressource introuvable (HTTP 404)."
    if status == 429:
        return "Nextmotion : trop de requêtes (HTTP 429) — réessaie dans un instant."
    if status >= 500:
        return f"Nextmotion est momentanément indisponible (HTTP {status})."
    return f"Nextmotion a refusé la requête (HTTP {status}) : {e.body}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : `GET /v4/users/me`, sans paramètre ni effet.

    Une clé vide est refusée AVANT le client : passée vide, `NextmotionClient`
    résoudrait `NEXTMOTION_API_KEY` dans l'environnement du serveur et testerait
    une autre clé que celle posée."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.nextmotion import NextmotionClient

    key = (fields or {}).get("key")
    if not isinstance(key, str) or not key.strip():
        raise connector_verify.NonAutorise("Nextmotion : clé API vide.")
    try:
        NextmotionClient(api_key=key.strip()).get_me()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(_upstream_message(e)) from e
        raise


_SAME = "__kind__"  # `Write.withheld` par défaut : celui du kind
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def _uuid(op: str, name: str, value: str) -> None:
    """Un id de chemin est un UUID — vérifié AVANT l'aperçu, comme le client le ferait
    à l'écriture : un aperçu ne promet jamais ce que l'écriture refuserait."""
    if not _UUID.fullmatch(str(value)):
        raise _bad(f"op={op!r} : `{name}` doit être un UUID — reçu {value!r}.")


@dataclass(frozen=True)
class Write:
    """Une écriture servie sous un `op` : sa cible, son corps, son appel.

    - `call(c, cible, corps)` — l'appel au client ; `cible` vaut le `clinic_id`, l'id de
      l'objet ou `None` selon `target`, `corps` vaut `None` quand l'op n'envoie rien ;
    - `target` — "clinic" (création sous une clinique), "item" (l'objet visé) ou "none" ;
    - `accepted` — la liste blanche d'ENTRÉE (`nextmotion_entrees`) ; `None` = aucun
      corps, et un `data` fourni est refusé ;
    - `required` — les champs de premier niveau que la spec exige ;
    - `many` — le corps est une LISTE d'objets, et la réponse une page ;
    - `optional_body` — `data` omis envoie un corps vide ;
    - `current(c, cible, corps)` — l'état montré par l'aperçu, déjà projeté (`None` = la
      lecture unitaire du kind quand la cible est l'objet) ;
    - `defaults` — des valeurs posées quand l'appelant ne les donne pas : les drapeaux de
      notification que la spec met à `true` partent à `false`, jamais de notification
      implicite ; `notify` nomme ces drapeaux, et l'aperçu dit lesquels préviendront ;
    - `key` / `shape` / `withheld` — la projection de la réponse quand ce n'est pas celle
      du kind (une conversion rend un patient, une répartition une page…)."""
    call: Callable[..., Any]
    target: str = "item"
    accepted: Optional[tuple] = None
    required: tuple = ()
    many: bool = False
    optional_body: bool = False
    current: Optional[Callable[..., Any]] = None
    defaults: dict = field(default_factory=dict)
    notify: tuple = ()
    key: Optional[str] = None
    shape: Optional[Callable[[Any], Any]] = None
    withheld: Optional[str] = _SAME


@dataclass(frozen=True)
class Kind:
    """Une ressource servie sous un `kind` d'outil : sa liste blanche, ses appels, ses
    filtres propres, ses écritures. `lister(c, clinic_id, **filtres, limit, offset)` et
    `lire(c, id)` sont `None` quand l'API n'a pas l'endpoint (l'op est alors refusée,
    nommément) ; `writes` associe un op d'écriture à son `Write`."""
    plural: str
    shape: Callable[[Any], Any]
    lister: Optional[Callable[..., Any]] = None
    lire: Optional[Callable[..., Any]] = None
    filters: tuple = ()
    withheld: Optional[str] = None
    writes: Optional[dict] = None


def _serve_kind(kinds: dict, kind: str, op: str, *, client: Callable[[], Any],
                clinic_id: Optional[str],
                item_id: Optional[str], filters: dict, limit: Optional[int],
                offset: Optional[int], fields: Optional[list],
                item_name: str = "item_id") -> dict:
    """`op` list | get d'un outil à `kind` : un filtre d'un autre kind est refusé, un
    argument que l'op n'utilise pas aussi (`is not None`), l'appel part ensuite.

    `client` est la fabrique (`_client`), passée par le module d'outils : c'est chez lui
    que la sonde de version-skew lit les méthodes appelées."""
    from .nextmotion_socle import _one, _page

    if kind not in kinds:
        raise _bad(f"kind inconnu : {kind!r}.")
    k = kinds[kind]
    for name, value in filters.items():
        if name not in k.filters and value is not None:
            raise _bad(f"kind={kind!r} n'utilise pas `{name}`.")
    own = {name: filters[name] for name in k.filters}
    if op == "list":
        if k.lister is None:
            raise _bad(f"kind={kind!r} : l'API Nextmotion n'a pas de liste — op='get'.")
        _need(op, clinic_id=clinic_id)
        _refuse_ignored(op, **{item_name: item_id})
        c = client()
        return _page(_run(lambda: k.lister(c, clinic_id, **own, **_paging(limit, offset))),
                     k.plural, k.shape, fields=fields, withheld=k.withheld)
    if op == "get":
        if k.lire is None:
            raise _bad(f"kind={kind!r} : l'API Nextmotion n'a pas de lecture unitaire — "
                       "op='list'.")
        _need(op, **{item_name: item_id})
        _refuse_ignored(op, clinic_id=clinic_id, limit=limit, offset=offset, fields=fields,
                        **own)
        c = client()
        return _one(_run(lambda: k.lire(c, item_id)), kind, k.shape, withheld=k.withheld)
    raise _bad("op doit être 'list' ou 'get'.")


def _crud(create: Optional[Callable[..., Any]], update: Optional[Callable[..., Any]],
          delete: Optional[Callable[..., Any]], accepted: tuple, required: tuple = (), *,
          accepted_update: Optional[tuple] = None,
          required_update: Optional[tuple] = None) -> dict:
    """Les écritures usuelles d'une ressource de clinique : `create` sous la clinique,
    `update` et `delete` sur l'objet. Un appel `None` = l'API n'a pas l'endpoint."""
    writes = {}
    if create is not None:
        writes["create"] = Write(create, "clinic", accepted, required)
    if update is not None:
        writes["update"] = Write(
            update, "item", accepted if accepted_update is None else accepted_update,
            required if required_update is None else required_update)
    if delete is not None:
        writes["delete"] = Write(delete, "item")
    return writes


def _serve(kinds: dict, kind: str, op: str, *, client: Callable[[], Any],
           clinic_id: Optional[str], item_id: Optional[str], filters: dict,
           limit: Optional[int], offset: Optional[int], fields: Optional[list],
           data: Any, dry_run: Optional[bool], item_name: str = "item_id") -> dict:
    """Un outil à `kind` qui lit ET écrit : list | get par `_serve_kind`, tout autre op
    par `_serve_write`. Un argument de l'autre famille est refusé (`is not None`)."""
    if kind not in kinds:
        raise _bad(f"kind inconnu : {kind!r}.")
    if op in ("list", "get"):
        _refuse_ignored(op, data=data, dry_run=dry_run)
        return _serve_kind(kinds, kind, op, client=client, clinic_id=clinic_id,
                           item_id=item_id, filters=filters, limit=limit, offset=offset,
                           fields=fields, item_name=item_name)
    return _serve_write(kinds, kind, op, client=client, clinic_id=clinic_id,
                        item_id=item_id, data=data, dry_run=dry_run,
                        unused={**filters, "limit": limit, "offset": offset,
                                "fields": fields}, item_name=item_name)


def _serve_write(kinds: dict, kind: str, op: str, *, client: Callable[[], Any],
                 clinic_id: Optional[str], item_id: Optional[str], data: Any,
                 dry_run: Optional[bool], unused: dict,
                 item_name: str = "item_id") -> dict:
    """Une écriture : cible et corps validés AVANT tout appel ; `dry_run` (True par
    défaut) rend l'aperçu sans appeler aucune méthode d'écriture ; la réponse repasse
    par la liste blanche de la ressource. `item_name` = le nom de l'argument d'id dans
    l'outil (`quote_id`…), pour que les refus le nomment."""
    from .nextmotion_socle import _one, _page

    k = kinds[kind]
    w = (k.writes or {}).get(op)
    if w is None:
        ops = ", ".join(["list", "get", *(k.writes or {})])
        raise _bad(f"kind={kind!r} : op={op!r} inconnue — ops : {ops}.")
    _refuse_ignored(op, **unused)
    if w.target == "clinic":
        _need(op, clinic_id=clinic_id)
        _refuse_ignored(op, **{item_name: item_id})
        target, target_name = clinic_id, "clinic_id"
    elif w.target == "item":
        _need(op, **{item_name: item_id})
        _refuse_ignored(op, clinic_id=clinic_id)
        target, target_name = item_id, item_name
    else:
        _refuse_ignored(op, clinic_id=clinic_id, **{item_name: item_id})
        target, target_name = None, None
    if target is not None:
        _uuid(op, target_name, target)
    body = _check_body(kind, op, w, data)
    if w.defaults and isinstance(body, dict):
        body = {**w.defaults, **body}
    c = client()
    if dry_run is None or dry_run:
        preview: dict = {"dry_run": True, "would": op, "kind": kind}
        if target_name:
            preview[target_name] = target
        current = w.current
        if current is None and w.target == "item" and k.lire is not None:
            def current(cl, i, _):
                return _one(_run(lambda: k.lire(cl, i)), kind, k.shape, withheld=k.withheld)
        if current is not None:
            preview.update(current(c, target, body))
        if body is not None:
            preview["data"] = _mask(body)
        if w.notify:
            preview["notifie_le_patient"] = [f for f in w.notify if (body or {}).get(f)]
        preview["note"] = f"Rien n'est écrit. Repasse avec dry_run=False pour {op}."
        return preview
    env = _run(lambda: w.call(c, target, body))
    key = w.key or (k.plural if w.many else kind)
    shape = w.shape or k.shape
    withheld = k.withheld if w.withheld == _SAME else w.withheld
    if env is None:  # 204
        ident = {target_name: target} if target_name else {}
        return {"deleted": True, **ident} if op == "delete" else {"done": True, "op": op,
                                                                  **ident}
    if w.many:
        return _page(env, key, shape, withheld=withheld)
    return _one(env, key, shape, withheld=withheld)


def _check_body(kind: str, op: str, w: Write, data: Any) -> Any:
    """`data` contre la liste blanche d'entrée de l'op : un champ que la liste ne nomme
    pas est REFUSÉ, nommément, à toute profondeur — jamais ignoré."""
    if w.accepted is None:
        _refuse_ignored(op, data=data)
        return None
    if data is None:
        if w.optional_body:
            return None
        raise _bad(f"op={op!r} exige `data` (kind={kind!r}).")
    if w.many:
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise _bad(f"op={op!r} : `data` doit être une liste d'objets.")
        for i, row in enumerate(data):
            _check_fields(kind, op, row, w.accepted, f"data[{i}]")
            _check_required(op, row, w.required, f"data[{i}]")
        return data
    if not isinstance(data, dict):
        raise _bad(f"op={op!r} : `data` doit être un objet.")
    _check_fields(kind, op, data, w.accepted, "data")
    _check_required(op, data, w.required, "data")
    return data


def _check_fields(kind: str, op: str, obj: dict, accepted: tuple, path: str) -> None:
    names = {f if isinstance(f, str) else f[0] for f in accepted}
    subs = {f[0]: f[1] for f in accepted if not isinstance(f, str)}
    unknown = sorted(set(obj) - names)
    if unknown:
        raise _bad(f"`{path}` : champ(s) non accepté(s) par kind={kind!r} op={op!r} : "
                   f"{', '.join('`' + u + '`' for u in unknown)}. Acceptés : "
                   f"{', '.join(sorted(names))}.")
    for name, sub in subs.items():
        value = obj.get(name)
        rows = value if isinstance(value, list) else [value]
        for i, row in enumerate(rows):
            if isinstance(row, dict):
                where = f"{path}.{name}" + (f"[{i}]" if isinstance(value, list) else "")
                _check_fields(kind, op, row, sub, where)


def _check_required(op: str, obj: dict, required: tuple, path: str) -> None:
    missing = [r for r in required if obj.get(r) is None or obj.get(r) == ""]
    if missing:
        raise _bad(f"op={op!r} : `{path}` exige {', '.join('`' + m + '`' for m in missing)}.")


def _mask(body: Any) -> Any:
    """L'aperçu redit le corps, sauf les champs qui portent un secret (`headers`)."""
    from .nextmotion_entrees import _SECRETS

    if isinstance(body, list):
        return [_mask(r) for r in body]
    if not isinstance(body, dict):
        return body
    return {k: ("<masqué>" if k in _SECRETS else v) for k, v in body.items()}

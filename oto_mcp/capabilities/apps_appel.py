"""Ce qu'une app OUVERTE demande — `POST /api/me/apps/{app_id}/call`, face REST seule.

Une app s'exécute chez son lecteur, dans un cadre isolé sans réseau : tout ce qu'elle
lit passe par le front, qui le relaie ICI, au nom du lecteur. C'est la seule porte :
le front n'appelle plus les tableaux pour une app.

**Ce qui est servi : des LECTURES** — `read_rows` et `read_aggregate`. Écrire une
valeur, ajouter une ligne, lancer un agent ou lire ses runs ne le sont pas : une app
agirait alors au nom d'un lecteur qui ne l'a pas demandé. Ces gestes viendront avec
un consentement du lecteur (oto-backend#1386) ; d'ici là, une autre action est une
entrée invalide (400), et rien ici n'écrit ni ne lance.

**La règle : droits d'une app = droits de son AUTEUR ∩ droits de son LECTEUR.**
L'auteur (`apps.updated_by`, le dernier à avoir écrit l'app) choisit ce qu'elle
touche ; le lecteur l'ouvre avec ses propres droits. Sans la règle, quelqu'un qui peut
éditer l'app sans lire un tableau le déclarerait et le ferait lire par la session d'un
collègue. Chaque appel juge donc, sur le tableau DÉCLARÉ au manifeste :
- **l'org du projet** de l'app : le tableau doit y vivre (`apps_manifeste.
  tableau_de_l_org`) — ni l'org active de l'auteur, ni celle du lecteur ;
- **l'auteur**, par l'ownership seul (`ownership.can_access` sur le NUMÉRO du
  tableau) — jamais par sa session : il n'est pas là, et une résolution par nom se
  ferait dans SON org active (ADR 0023) ;
- **le lecteur**, en exécutant pour lui les capacités existantes (`_list_rows`,
  `_aggregate`) : leurs règles, pas une copie.
Auteur inconnu ou qui a perdu l'accès : refus nommé, rien n'est servi.

**Ce que l'app voit est borné par son manifeste**, ici et non plus dans le front
seul : champs déclarés (projection, filtres, tri, regroupement, mesures). Un manifeste
stocké est déjà normalisé (`apps_manifeste.forme`) et ses tableaux sont des numéros.

**Session interactive seule** (`allow_api_token=False`) : aucun jeton `oto_`,
délégation d'agent hébergé comprise — un agent n'est jamais le lecteur d'une app.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .. import db, ownership
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .apps_manifeste import org_du_projet, tableau_de_l_org
from .registry import CAPABILITIES

PROJECT_RTYPE = "project"
MAX_ROWS = 200
# Ce qu'une ligne porte toujours, en plus des champs déclarés : de quoi la désigner
# et la dater.
_META = frozenset({"_id", "_revision", "_created_at", "_updated_at"})
_DATES = frozenset({"_created_at", "_updated_at"})
_AGG_OPS = frozenset({"count_rows", "count", "sum", "avg", "min", "max"})

Action = Literal["read_rows", "read_aggregate"]


class AppCallInput(BaseModel):
    app_id: int
    action: Action
    table: Optional[str] = Field(default=None, description="The table's name in the manifest.")
    # read_rows : limit, offset, order_by, order_dir, filter, q — read_aggregate :
    # group_by, metrics, filter.
    options: dict = Field(default_factory=dict)


class AppCallOut(BaseModel):
    rows: Optional[list[dict]] = None
    total: Optional[int] = None
    groups: Optional[list[dict]] = None


def _refus(status: int, code: str, msg: str) -> AuthzDenied:
    return AuthzDenied(status, code, msg)


def _app_pour(ctx: ResolvedCtx, app_id: int) -> dict:
    app = db.apps.get_for_call(int(app_id))
    if (app is None or app.get("archived_at") is not None
            or not ownership.can_access(ctx.sub, PROJECT_RTYPE, str(app["project_id"]), "read")):
        raise _refus(404, "unknown_app", f"App {app_id} doesn't exist or isn't shared with you.")
    if not app.get("updated_by"):
        raise _refus(403, "app_author_unknown",
                     "Nobody is known to have written this app, so it can't act for anyone.")
    return app


def _table(app: dict, alias: Optional[str]) -> tuple[str, dict]:
    t = (app["manifest"].get("tables") or {}).get(alias or "")
    if not isinstance(t, dict) or not str(t.get("table") or "").isdigit():
        raise _refus(400, "undeclared_table", f"This app doesn't declare a table named “{alias}”.")
    ns = str(t["table"])
    if not tableau_de_l_org(ns, org_du_projet(app["project_id"])):
        raise _refus(403, "table_outside_project_org",
                     f"Table {ns} no longer lives in this app's project organization.")
    if not ownership.can_access(app["updated_by"], ownership.TYPE_RESSOURCE_DATASTORE, ns, "read"):
        raise _refus(403, "app_author_lost_access",
                     f"This app's author can no longer read table {ns}: the app can't either.")
    return ns, t


def _lisible(t: dict, champ: str) -> bool:
    """Un champ déclaré — ou l'élément d'une liste par sa colonne (`tags[]`,
    `contacts[].role`). Sans liste de champs, tout ce que le lecteur voit."""
    fields = t.get("fields")
    return fields is None or champ.split("[")[0].split(".")[0] in fields


def _filtre(t: dict, raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _refus(400, "invalid_filter", "A filter is an object of field → value.")
    caches = sorted(k for k in raw if k not in _DATES and not _lisible(t, k))
    if caches:
        raise _refus(403, "undeclared_field",
                     f"An app can only filter on the fields it declares (not {', '.join(caches)}).")
    return json.dumps(raw) if raw else None


def _entier(o: dict, cle: str, defaut: int) -> int:
    """Une option numérique : absente → `defaut`, autre chose qu'un entier → 400."""
    v = o.get(cle)
    if v is None:
        return defaut
    if isinstance(v, bool) or not isinstance(v, int):
        raise _refus(400, "invalid_options", f"`options.{cle}` must be an integer.")
    return v


def _projeter(t: dict, row: dict) -> dict:
    fields = t.get("fields")
    if fields is None:
        return row
    return {k: v for k, v in row.items() if k in fields or k in _META}


def _call(ctx: ResolvedCtx, inp: AppCallInput) -> dict:
    from .datastore import rows as R

    app = _app_pour(ctx, inp.app_id)
    o = inp.options or {}
    ns, t = _table(app, inp.table)

    if inp.action == "read_rows":
        order_by = o.get("order_by")
        if order_by is not None and (not isinstance(order_by, str)
                                     or (order_by not in _DATES and not _lisible(t, order_by))):
            raise _refus(403, "undeclared_field", "An app can only sort on a field it declares.")
        q = o.get("q")
        if q and t.get("fields") is not None:
            raise _refus(403, "search_needs_all_fields",
                         "Search runs over every field: it needs a table declared without a field list.")
        page = R._list_rows(ctx, R.ListRowsInput(
            datastore=ns, limit=min(max(_entier(o, "limit", 50), 1), MAX_ROWS),
            offset=max(_entier(o, "offset", 0), 0), order_by=order_by,
            order_dir="asc" if o.get("order_dir") == "asc" else "desc",
            q=q if isinstance(q, str) and q else None, filter=_filtre(t, o.get("filter"))))
        return {"rows": [_projeter(t, r) for r in page.get("rows") or []],
                "total": page.get("total")}

    # read_aggregate
    group_by = o.get("group_by")
    if group_by is not None and (not isinstance(group_by, str) or not _lisible(t, group_by)):
        raise _refus(403, "undeclared_field", "An app can only group on a field it declares.")
    metrics = o.get("metrics") or [{"op": "count_rows", "label": "count"}]
    if not isinstance(metrics, list) or len(metrics) > 10:
        raise _refus(400, "invalid_metrics", "Metrics are a list of at most 10.")
    propres = []
    for i, m in enumerate(metrics):
        m = m if isinstance(m, dict) else {}
        if m.get("op") not in _AGG_OPS:
            raise _refus(400, "invalid_metrics", f"Unknown metric “{m.get('op')}”.")
        if m.get("field") is not None and (not isinstance(m["field"], str) or not _lisible(t, m["field"])):
            raise _refus(403, "undeclared_field", "An app can only measure the fields it declares.")
        # Pas de `where` par métrique : il filtrerait sur un champ non déclaré.
        propres.append({"op": m["op"], **({"field": m["field"]} if m.get("field") else {}),
                        "label": m.get("label") if isinstance(m.get("label"), str) else f"m{i}"})
    out = R._aggregate(ctx, R.AggregateInput(
        datastore=ns, group_by=group_by, metrics=json.dumps(propres),
        filter=_filtre(t, o.get("filter"))))
    return {"groups": out.get("groups") or []}


CAPABILITIES += [
    Capability(
        key="me.app.call", handler=_call, Input=AppCallInput, Output=AppCallOut,
        authz=ORG_MEMBER,
        description=(
            "What an open app asks, relayed by the front on behalf of its viewer: read "
            "rows or aggregates of a table the app declares, bounded to its declared "
            "fields. Allowed only when BOTH the app's author and its viewer may read "
            "it, and the table lives in the app's project organization. Interactive "
            "sessions only (no API token)."
        ),
        mcp=None,
        rest=RestBinding("POST", "/api/me/apps/{app_id}/call", allow_api_token=False),
    ),
]

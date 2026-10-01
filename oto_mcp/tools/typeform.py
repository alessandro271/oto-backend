"""Typeform — formulaires en ligne, en LECTURE SEULE : espaces de travail,
formulaires (et leurs questions), réponses.

Enveloppe `oto.tools.typeform.TypeformClient` (jeton personnel en Bearer).
Credential résolu par appel via `access.resolve_credential_fields("typeform")`
(byo user OU org, pas de clé plateforme) : `key` (secret) et `region` (us par
défaut, eu, eu2), qui choisit l'hôte du data center.

Trois outils, un par objet :
- `typeform_workspaces` — les espaces de travail du jeton ;
- `typeform_forms` (op list|get) — les formulaires, la définition d'un formulaire ;
- `typeform_responses` — les réponses d'un formulaire, filtrées et paginées.

**Ce que la couche outil ajoute au transport** :
1. Des vues resserrées par défaut (ADR 0047, `full=True` rend le brut) : une
   liste rend de quoi choisir, une définition rend ses questions sans écrans,
   thème ni logique — et la réponse NOMME ce qu'elle a retiré (`omitted`).
2. **Les réponses lisibles.** L'API rend `answers[]` dans un ordre quelconque,
   chacune repérée par l'id de sa question, la valeur rangée sous une clé qui
   dépend de son type. `typeform_responses` lit la définition du formulaire
   une fois par page et rend chaque réponse en `{intitulé de la question:
   valeur}` ; deux questions au même intitulé sont départagées par leur id,
   jamais écrasées.
3. **Le piège du data center, fermé.** Lues hors de la région du compte, les
   réponses reviennent VIDES sans erreur. La définition du formulaire porte
   l'hôte de ses réponses (`_links.responses`) : s'il ne correspond pas à la
   région posée, l'outil REFUSE en nommant la bonne région, au lieu de rendre
   un zéro crédible.

**Aucun argument n'est retenu en silence** : un argument qu'un `op` n'utilise
pas est refusé (`_refuse_ignored`, patron tally/claap).

Les appels au client sont écrits en clair (`_client().list_forms(…)`) pour la
sonde de version (`test_tools_client_methods_exist`).

Vérifié contre la référence publique (Create API, Responses API, page « EU
Responses Data Center ») ; **pas testé en live** — aucun jeton Typeform
disponible à l'écriture.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

if TYPE_CHECKING:  # l'annotation de `_client()` seulement — jamais évaluée
    from oto.tools.typeform import TypeformClient

#: Où l'utilisateur crée son jeton, dans SON compte Typeform.
WHERE_TO_CREATE = "Typeform → Account → Personal tokens"

#: Borne d'une page de réponses servie à l'agent. L'amont en accepte 1000, mais
#: les réponses ne se projettent pas (un texte long est la donnée) : la page
#: est la seule borne de taille, et `total_items` dit combien il en reste.
RESPONSES_MAX_PAGE = 200
RESPONSES_DEFAULT_PAGE = 25

#: Ce que la vue resserrée d'un formulaire retire (rendu avec `full=True`).
FORM_OMITTED = ("welcome_screens", "thankyou_screens", "logic", "theme",
                "settings", "attachments/layouts")
#: Ce que la vue resserrée d'une réponse retire.
RESPONSE_OMITTED = ("metadata", "landing_id", "token", "answers[].field.type")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided: Any) -> None:
    """Un argument fourni que CET op n'utilise pas est une erreur d'intention —
    sinon `op="get"` avec `search=` rendrait un formulaire en laissant croire
    que la recherche a filtré."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


def _region(value: Any) -> str:
    """`region` du credential → région du client. Vide = us. Une valeur hors du
    jeu déclaré est refusée à la pose ; ici aussi, plutôt que de viser un hôte
    au hasard."""
    from oto.tools.typeform import REGIONS

    region = str(value or "us").strip().lower()
    if region not in REGIONS:
        raise _bad(f"Typeform: unknown data center {value!r} on this connector — "
                   f"expected one of {', '.join(REGIONS)}.")
    return region


def upstream_message(e: Any, what: str = "") -> str:
    """Un refus de Typeform (`UpstreamHTTPError`) en consigne actionnable."""
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("description") or body.get("message") or body.get("code") or e.body
    if status == 401:
        return (f"Typeform rejected the token (401): it is unknown, revoked, or "
                f"belongs to another data center — tokens of « eu2 » "
                f"(api.typeform.eu) are distinct. Create one in {WHERE_TO_CREATE}.")
    if status == 403:
        return (f"Typeform refused this read (403): the token lacks the scope it "
                f"needs ({what or 'forms:read, responses:read, workspaces:read'}) "
                f"— generate a token with it in {WHERE_TO_CREATE}. {detail}")
    if status == 404:
        return f"Typeform: not found (404){' — ' + str(detail) if detail else ''}."
    return f"Typeform refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : `GET /forms?page_size=1`, sans effet de
    bord, sur l'hôte de la région posée. Couvre `auth` et le scope `forms:read`,
    celui sans lequel aucune réponse ne se lit lisiblement ; un 401/403 lève
    `UpstreamHTTPError`, que le classement de la sonde lit par son code."""
    from oto.tools.typeform import TypeformClient

    TypeformClient(access_token=fields["key"],
                   region=_region(fields.get("region"))).list_forms(page_size=1)


def _client() -> TypeformClient:
    """Le client Typeform pour le credential de CET appelant, sur l'hôte de sa
    région. Import réel dans le corps : les tests remplacent le client, et la
    sonde de version lit l'annotation de retour."""
    from oto.tools.typeform import TypeformClient

    fields = access.resolve_credential_fields("typeform")
    return TypeformClient(access_token=fields["key"],
                          region=_region(fields.get("region")))


def _run(fn, what: str = "") -> Any:
    """4xx → refus nommé ; 429 et 5xx restent ce qu'ils sont (réessayables)."""
    from oto.tools.common import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e)) from None
    except UpstreamHTTPError as e:
        if 400 <= e.status_code < 500 and e.status_code != 429:
            raise _bad(upstream_message(e, what)) from None
        raise


# ---------------------------------------------------------------------------
# Vues resserrées
# ---------------------------------------------------------------------------

def _slim_workspace(ws: dict) -> dict:
    forms = ws.get("forms")
    if isinstance(forms, list):          # l'exemple de la référence en fait une liste
        forms = forms[0] if forms else {}
    out = {"id": ws.get("id"), "name": ws.get("name"), "shared": ws.get("shared"),
           "forms_count": (forms or {}).get("count") if isinstance(forms, dict) else None,
           "account_id": ws.get("account_id")}
    return {k: v for k, v in out.items() if v is not None}


def _slim_form_row(f: dict) -> dict:
    out = {"id": f.get("id"), "title": f.get("title"),
           "last_updated_at": f.get("last_updated_at"),
           "created_at": f.get("created_at"),
           "is_public": (f.get("settings") or {}).get("is_public"),
           "url": (f.get("_links") or {}).get("display")}
    return {k: v for k, v in out.items() if v is not None}


def _slim_field(field: dict) -> dict:
    """Une question : de quoi la reconnaître dans une réponse (id, ref) et
    l'interpréter (type, intitulé, choix). Les groupes gardent leurs sous-questions."""
    props = field.get("properties") or {}
    out: Dict[str, Any] = {"id": field.get("id"), "ref": field.get("ref"),
                           "type": field.get("type"), "title": field.get("title")}
    if props.get("description"):
        out["description"] = props["description"]
    if (field.get("validations") or {}).get("required"):
        out["required"] = True
    choices = props.get("choices")
    if isinstance(choices, list) and choices:
        out["choices"] = [ch.get("label") for ch in choices if isinstance(ch, dict)]
        if props.get("allow_multiple_selection"):
            out["multiple"] = True
        if props.get("allow_other_choice"):
            out["other_allowed"] = True
    sub = props.get("fields")
    if isinstance(sub, list) and sub:
        out["fields"] = [_slim_field(s) for s in sub if isinstance(s, dict)]
    return {k: v for k, v in out.items() if v is not None}


def _slim_form(form: dict) -> dict:
    links = form.get("_links") or {}
    out = {"id": form.get("id"), "title": form.get("title"),
           "language": form.get("language") or (form.get("settings") or {}).get("language"),
           "url": links.get("display"),
           "fields": [_slim_field(f) for f in form.get("fields") or [] if isinstance(f, dict)],
           "hidden": form.get("hidden") or None,
           "variables": form.get("variables") or None,
           "omitted": list(FORM_OMITTED)}
    return {k: v for k, v in out.items() if v is not None}


def _flat_fields(fields: Any) -> List[dict]:
    """Toutes les questions, sous-questions de groupe comprises (une réponse
    pointe la sous-question, pas le groupe)."""
    out: List[dict] = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        out.append(f)
        sub = (f.get("properties") or {}).get("fields")
        if isinstance(sub, list):
            out.extend(_flat_fields(sub))
    return out


def _labels(form: Optional[dict]) -> Dict[str, str]:
    """id de question → clé lisible : l'intitulé ; deux intitulés égaux sont
    départagés par l'id, jamais fusionnés (l'un écraserait l'autre)."""
    if not form:
        return {}
    flat = [f for f in _flat_fields(form.get("fields")) if f.get("id")]
    titles = [str(f.get("title") or "").strip() for f in flat]
    labels = {}
    for f, title in zip(flat, titles):
        if not title:
            labels[f["id"]] = f.get("ref") or f["id"]
        elif titles.count(title) > 1:
            labels[f["id"]] = f"{title} [{f['id']}]"
        else:
            labels[f["id"]] = title
    return labels


def answer_value(answer: dict) -> Any:
    """La valeur d'une réponse, quel que soit son type : rangée sous la clé qui
    porte le nom de son `type` (`text`, `choice`, `choices`, `number`…). Un
    choix rend son libellé, un choix multiple la liste des libellés ; un type
    inconnu rend sa charge telle quelle plutôt que rien."""
    kind = answer.get("type")
    value = answer.get(kind) if kind else None
    if kind == "choice" and isinstance(value, dict):
        return value.get("label") if value.get("label") is not None else value.get("other")
    if kind == "choices" and isinstance(value, dict):
        labels = list(value.get("labels") or [])
        if value.get("other"):
            labels.append(value["other"])
        return labels
    if value is None:
        return {k: v for k, v in answer.items() if k not in ("field", "type")} or None
    return value


def _shape_response(item: dict, labels: Dict[str, str]) -> dict:
    answers: Dict[str, Any] = {}
    for a in item.get("answers") or []:
        if not isinstance(a, dict):
            continue
        field = a.get("field") or {}
        fid = field.get("id")
        key = labels.get(fid) or field.get("ref") or fid or "?"
        answers[key] = answer_value(a)
    out: Dict[str, Any] = {"response_id": item.get("response_id"),
                           "submitted_at": item.get("submitted_at"),
                           "landed_at": item.get("landed_at"),
                           "answers": answers}
    if item.get("hidden"):
        out["hidden"] = item["hidden"]
    score = (item.get("calculated") or {}).get("score")
    if score:
        out["score"] = score
    variables = {v.get("key"): v.get(v.get("type")) for v in item.get("variables") or []
                 if isinstance(v, dict) and v.get("key")}
    if variables:
        out["variables"] = variables
    return {k: v for k, v in out.items() if v is not None}


def _host(url: Any) -> Optional[str]:
    return urlsplit(url).hostname if isinstance(url, str) and url else None


def _check_region(form: dict, client: Any) -> None:
    """La définition dit où vivent les réponses (`_links.responses`). Un autre
    hôte que celui du client = des réponses qui reviendraient vides : refuser."""
    from oto.tools.typeform import REGIONS

    expected = _host((form.get("_links") or {}).get("responses"))
    current = _host(getattr(client, "BASE_URL", None))
    if not expected or not current or expected == current:
        return
    by_host = {_host(u): r for r, u in REGIONS.items()}
    right = by_host.get(expected)
    raise _bad(
        f"Typeform: this form's responses are stored on {expected}, but the "
        f"connector reads {current} — its responses would come back EMPTY. "
        + (f"Set the Typeform connector's data center to « {right} »."
           if right else "This data center is not one the connector knows."))


def register(mcp: FastMCP) -> None:
    connector_verify.register("typeform", _verify)

    @mcp.tool()
    def typeform_workspaces(
        search: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        full: bool = False,
    ) -> dict:
        """Typeform workspaces the token can access (read only), across
        organizations — each with its id (to filter `typeform_forms`) and its
        number of forms.

        Returns `{total_items, page_count, page, workspaces: [{id, name, shared,
        forms_count, account_id}]}`; `full=True` returns Typeform's raw page.

        Args:
            search: only workspaces whose name contains this text.
            page: 1-based page number (default 1).
            page_size: 1-200, default 10.
            full: raw payload.
        """
        res = _run(lambda: _client().list_workspaces(
            search=search, page=page, page_size=page_size), "workspaces:read")
        if full:
            return res
        return {"total_items": res.get("total_items"), "page_count": res.get("page_count"),
                "page": page or 1,
                "workspaces": [_slim_workspace(w) for w in res.get("items") or []
                               if isinstance(w, dict)]}

    @mcp.tool()
    def typeform_forms(
        op: Literal["list", "get"] = "list",
        form_id: Optional[str] = None,
        search: Optional[str] = None,
        workspace_id: Optional[str] = None,
        sort_by: Optional[Literal["created_at", "last_updated_at"]] = None,
        order_by: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        full: bool = False,
    ) -> dict:
        """Typeform forms (read only): list them, or read one form's questions.

        `op`:
        - `list` — forms of the account, public and private: `{total_items,
          page_count, page, forms: [{id, title, last_updated_at, created_at,
          is_public, url}]}`.
        - `get` — one form (`form_id`): its questions `fields: [{id, ref, type,
          title, description?, required?, choices?, multiple?, fields? (group)}]`,
          `hidden` field names and `variables` — what you need to read its
          responses. Screens, logic, theme and settings are left out
          (`omitted`); `full=True` returns the whole definition.

        Args:
            op: list | get.
            form_id: get — the form id (last segment of its URL, e.g. `u6nXL7`
                in `…typeform.com/to/u6nXL7`).
            search: list — only forms containing this text.
            workspace_id: list — only this workspace's forms
                (`typeform_workspaces`).
            sort_by: list — created_at | last_updated_at.
            order_by: list — asc | desc.
            page: list — 1-based page number (default 1).
            page_size: list — 1-200, default 10.
            full: raw payload.
        """
        if op == "list":
            _refuse_ignored(op, "use op='get' to read one form", form_id=form_id)
            res = _run(lambda: _client().list_forms(
                search=search, page=page, page_size=page_size,
                workspace_id=workspace_id, sort_by=sort_by, order_by=order_by),
                "forms:read")
            if full:
                return res
            return {"total_items": res.get("total_items"),
                    "page_count": res.get("page_count"), "page": page or 1,
                    "forms": [_slim_form_row(f) for f in res.get("items") or []
                              if isinstance(f, dict)]}
        if op == "get":
            if not form_id:
                raise _bad("op='get': `form_id` is required.")
            _refuse_ignored(op, "it only applies to op='list'", search=search,
                            workspace_id=workspace_id, sort_by=sort_by,
                            order_by=order_by, page=page, page_size=page_size)
            form = _run(lambda: _client().get_form(form_id), "forms:read")
            return form if full else _slim_form(form)
        raise _bad(f"invalid `op`: {op!r} (expected: list | get).")

    @mcp.tool()
    def typeform_responses(
        form_id: str,
        page_size: Optional[int] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
        response_type: Optional[List[Literal["completed", "partial", "started"]]] = None,
        sort: Optional[str] = None,
        query: Optional[str] = None,
        included_response_ids: Optional[List[str]] = None,
        excluded_response_ids: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        answered_fields: Optional[List[str]] = None,
        titles: bool = True,
        full: bool = False,
    ) -> dict:
        """Responses to a Typeform form (read only), newest first.

        Each response is returned readable: `{response_id, submitted_at,
        landed_at, answers: {<question title>: value}, hidden?, score?,
        variables?}` — a choice gives its label, a multiple choice the list of
        labels. Two questions with the same title are told apart by their id.
        Titles come from the form definition, read once per call (scope
        `forms:read`); `titles=False` skips it and keys answers by field ref.
        Metadata (browser, referer) is left out; `full=True` returns the raw page.

        To page, pass `next_before` (given when the page is full, in the
        default order) as `before` to get the next, older page. `total_items`
        is the number of matching responses. Responses from the last ~30 minutes
        may not be listed yet.

        Args:
            form_id: the form id (`typeform_forms`).
            page_size: 1-200, default 25.
            since: submitted on or after — ISO 8601 UTC to the second
                (`2026-09-01T00:00:00`) or Unix seconds.
            until: submitted on or before, same formats.
            before: cursor — responses older than this one (`next_before`).
            after: cursor — responses newer than this one (a response token).
            response_type: completed (default) | partial | started, one or
                several. Also sets the date `since`/`until` filter on:
                submission, last save, or landing.
            sort: `<field>,<asc|desc>`, default `submitted_at,desc`.
            query: exact phrase searched in answers, hidden fields, variables.
            included_response_ids: only these response ids.
            excluded_response_ids: all but these response ids.
            fields: question ids — only these answers are returned.
            answered_fields: question ids — only responses answering one of them.
            titles: key answers by question title (default) rather than ref.
            full: raw payload.
        """
        size = RESPONSES_DEFAULT_PAGE if page_size is None else page_size
        if not 1 <= size <= RESPONSES_MAX_PAGE:
            raise _bad(f"`page_size` must be between 1 and {RESPONSES_MAX_PAGE} "
                       f"(got {page_size}); page with `before`.")
        if before and after:
            raise _bad("pass `before` OR `after`, not both.")
        client = _client()
        form = None
        if titles and not full:
            form = _run(lambda: client.get_form(form_id), "forms:read")
            _check_region(form, client)
        res = _run(lambda: client.list_responses(
            form_id, page_size=size, since=since, until=until, after=after,
            before=before, included_response_ids=included_response_ids,
            excluded_response_ids=excluded_response_ids,
            response_type=response_type, sort=sort, query=query, fields=fields,
            answered_fields=answered_fields), "responses:read")
        if full:
            return res
        items = [i for i in res.get("items") or [] if isinstance(i, dict)]
        labels = _labels(form)
        out: Dict[str, Any] = {
            "form_id": form_id, "total_items": res.get("total_items"),
            "responses": [_shape_response(i, labels) for i in items],
            "omitted": list(RESPONSE_OMITTED)}
        if form:
            out["form_title"] = form.get("title")
        # Le curseur de la page suivante : le jeton du dernier élément, tant que
        # la page est pleine et que l'ordre est celui par défaut (le plus récent
        # d'abord) — c'est le parcours que décrit la référence.
        if items and len(items) == size and not after and not sort:
            out["next_before"] = items[-1].get("token")
        return out

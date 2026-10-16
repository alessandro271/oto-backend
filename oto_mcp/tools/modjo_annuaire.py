"""Modjo — call reviews, the CRM objects Modjo mirrors, the workspace directory,
and the question to Modjo's AI.

Second module of the `modjo` connector (`tools/modjo.py` serves the calls; the
shared base lives in `modjo_socle.py`). Four tools:

- `modjo_reviews` — call reviews (coaching scorecards) and their answers;
- `modjo_crm` — accounts, contacts and deals Modjo mirrors from the connected
  CRM, and a deal's AI summary;
- `modjo_workspace` — tags, topics, members, teams, CRM filling templates;
- `modjo_ask` — a question to Modjo's AI about one call, deal or account,
  answered in a JSON shape the caller gives.

The first three only read and are declared so (`LECTURE`). `modjo_ask` writes
nothing either, but it runs a model at Modjo for up to five minutes: it is not
offered to recipes.

**A list is an index** (ADR 0047): each list op serves its records without the
fields an agent does not decide on — timestamps, colours, CRM identifiers served
twice (`crmIdentifier` repeats `crmId`), CRM links, phone numbers and timezones of
members, the ids and positions of review answers. A DENYLIST of named keys, read
in Modjo's public OpenAPI v2: a field Modjo adds tomorrow is served, never lost.
`full=True` returns the records as Modjo sends them; one object (`op="account"`,
`op="user"`…) is always served whole.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Union

from fastmcp import FastMCP

from .. import output_projection
from .lecture import LECTURE
from .modjo_socle import _bad, _client, hors_op, instant_utc, need, paged, run

_WORKSPACE_PERMIS = {
    "tags": ("page", "size", "full"), "topics": ("page", "size", "full"),
    "users": ("email", "page", "size", "full"), "user": ("user_id",),
    "teams": ("name", "page", "size", "full"), "team": ("team_id",),
    "team_members": ("team_id", "page", "size", "full"),
    "crm_templates": ("status", "page", "size", "full"),
    "crm_template": ("template_uuid",),
    "crm_template_fields": ("template_uuid", "page", "size", "full"),
}
_CRM_PERMIS = {
    "accounts": ("name", "page", "size", "full"), "account": ("account_id",),
    "contacts": ("name", "page", "size", "full"), "contact": ("contact_id",),
    "deals": ("name", "account_id", "status", "page", "size", "full"),
    "deal_summary": ("deal_id",),
}

#: What each list's DEFAULT view removes from every record (`full=True` keeps it).
_DATES = ("createdOn", "modifiedOn")
_CRM = ("crmIdentifier", "crmLink")
_MEMBRE = _DATES + ("phoneNumber", "timezone")
_RETIRE = {
    "accounts": _DATES + _CRM,
    "contacts": _DATES + _CRM,
    "deals": _DATES + _CRM,
    "tags": _DATES + ("color",),
    "topics": _DATES + ("color", "slug"),
    "users": _MEMBRE,
    "team_members": _MEMBRE,
    "teams": _DATES,
    "crm_templates": _DATES,
    "crm_template_fields": _DATES,
    "reviews": ("modifiedOn",),
}


def _shape(res: Any, op: str, full: bool) -> Any:
    """The default view of one list page: the `data` records without the keys
    `_RETIRE[op]` names; the envelope (`pagination`) is untouched. A review keeps
    each answer's question title, rating and feedback."""
    if full:
        return res
    vue = output_projection.project(res, items_path="data", item_drop=_RETIRE[op])
    if op == "reviews" and isinstance(vue, dict) and isinstance(vue.get("data"), list):
        vue["data"] = [_review(r) for r in vue["data"]]
    return vue


def _review(r: Any) -> Any:
    if not isinstance(r, dict):
        return r
    out = dict(r)
    if isinstance(r.get("template"), dict):
        out["template"] = r["template"].get("title")
    if isinstance(r.get("answers"), list):
        out["answers"] = [
            {"question": (a.get("question") or {}).get("title")
             if isinstance(a.get("question"), dict) else a.get("question"),
             "rating": a.get("rating"), "feedback": a.get("feedback")}
            if isinstance(a, dict) else a
            for a in r["answers"]]
    return out


def _check_op(op: str, permis: Dict[str, tuple], fournis: Dict[str, Any]) -> None:
    if op not in permis:
        raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(permis)}).")
    hors_op(op, permis[op], **fournis)


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=LECTURE)
    def modjo_reviews(
        start_date_time: Optional[str] = None,
        end_date_time: Optional[str] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        size: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Modjo — call reviews (coaching scorecards), read only: template, reviewee,
        overall rating and the answer to each question (ratings 0-4, -1 = not
        applicable). Newest first; `order="asc"` for an incremental sync. Private
        reviews are returned too (`isPrivate` is informative). Paged
        (`pagination.next_page`). The default view gives each review's template
        TITLE and, per answer, the question title, rating and feedback;
        `full=True` returns the reviews as Modjo sends them.

        Args:
            start_date_time: reviews created from this ISO 8601 date-time (with `Z`
                or an offset, sent in UTC; a date alone = midnight UTC).
            end_date_time: reviews created until this date-time.
            order: asc | desc (default desc).
            page: page number, from 1.
            size: items per page, 1-100 (default 25).
            full: True = the raw reviews (answer ids and positions, template id).
        """
        start_date_time = instant_utc(start_date_time, "start_date_time")
        end_date_time = instant_utc(end_date_time, "end_date_time")
        return _shape(paged(run(lambda: _client().list_call_reviews(
            start_date_time=start_date_time, end_date_time=end_date_time,
            order=order, page=page, size=size))), "reviews", full)

    @mcp.tool(annotations=LECTURE)
    def modjo_crm(
        op: Literal["accounts", "account", "contacts", "contact", "deals",
                    "deal_summary"] = "deals",
        name: Optional[str] = None,
        account_id: Optional[int] = None,
        contact_id: Optional[int] = None,
        deal_id: Optional[int] = None,
        status: Optional[Literal["Open", "Closed won", "Closed lost", "Closed",
                                 "Deleted"]] = None,
        page: Optional[int] = None,
        size: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Modjo — the CRM objects Modjo mirrors from the connected CRM, read only:
        accounts, contacts, deals (status, stage, amount, close date, owner), and
        the AI summary Modjo built for a deal across its calls. Each carries its
        CRM id and link. Their ids filter `modjo_calls op=list`.

        `op`: `accounts` | `contacts` | `deals` (lists, paged, `name` filter) ·
        `account` | `contact` (one, by id) · `deal_summary` (`deal_id`). A list
        leaves out timestamps and CRM links (`full=True` for everything); one
        object is served whole.

        Args:
            op: see above.
            name: accounts / contacts / deals — filter by name.
            account_id: account — its id; deals — only this account's deals.
            contact_id: contact — its id.
            deal_id: deal_summary — the deal id.
            status: deals — Open | Closed won | Closed lost | Closed | Deleted.
            page: lists — page number, from 1.
            size: lists — items per page, 1-100 (default 25).
            full: lists — True = the raw records (timestamps, CRM links).
        """
        _check_op(op, _CRM_PERMIS, dict(name=name, account_id=account_id,
                                        contact_id=contact_id, deal_id=deal_id,
                                        status=status, page=page, size=size, full=full))
        c = _client()
        if op == "accounts":
            return _shape(paged(run(lambda: c.list_accounts(
                name=name, page=page, size=size))), op, full)
        if op == "contacts":
            return _shape(paged(run(lambda: c.list_contacts(
                name=name, page=page, size=size))), op, full)
        if op == "deals":
            return _shape(paged(run(lambda: c.list_deals(
                name=name, account_id=account_id, status=status, page=page,
                size=size))), op, full)
        if op == "account":
            need(op, account_id=account_id)
            return run(lambda: c.get_account(account_id), "no account with this id")
        if op == "contact":
            need(op, contact_id=contact_id)
            return run(lambda: c.get_contact(contact_id), "no contact with this id")
        need(op, deal_id=deal_id)
        return run(lambda: c.get_deal_summary(deal_id),
                   "no deal with this id, or no summary for it yet")

    @mcp.tool(annotations=LECTURE)
    def modjo_workspace(
        op: Literal["tags", "topics", "users", "user", "teams", "team", "team_members",
                    "crm_templates", "crm_template", "crm_template_fields"] = "users",
        email: Optional[str] = None,
        user_id: Optional[int] = None,
        name: Optional[str] = None,
        team_id: Optional[int] = None,
        status: Optional[Literal["pending", "published"]] = None,
        template_uuid: Optional[str] = None,
        page: Optional[int] = None,
        size: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Modjo — the workspace, read only: tags (ids for `modjo_calls
        op=add_tag`), topics detected in calls, members (role user, manager or
        admin; `email` finds one, whose id filters `modjo_calls op=list`), teams
        and their members, and the CRM filling templates (which calls get CRM
        fields filled by the AI, and each field's prompt). Members, teams and
        webhooks are never administered here.

        `op`: `tags` | `topics` | `users` | `teams` | `team_members` |
        `crm_templates` | `crm_template_fields` (lists, paged) · `user` | `team` |
        `crm_template` (one). A list leaves out timestamps, colours, and members'
        phone numbers and timezones (`full=True` for everything); one object is
        served whole.

        Args:
            op: see above.
            email: users — exact email of one member.
            user_id: user — the member id.
            name: teams — filter by name.
            team_id: team / team_members — the team id.
            status: crm_templates — pending | published.
            template_uuid: crm_template / crm_template_fields — the template UUID.
            page: lists — page number, from 1.
            size: lists — items per page, 1-100 (default 25).
            full: lists — True = the raw records.
        """
        _check_op(op, _WORKSPACE_PERMIS, dict(email=email, user_id=user_id, name=name,
                                              team_id=team_id, status=status,
                                              template_uuid=template_uuid, page=page,
                                              size=size, full=full))
        c = _client()
        if op == "tags":
            return _shape(paged(run(lambda: c.list_tags(page=page, size=size))), op, full)
        if op == "topics":
            return _shape(paged(run(lambda: c.list_topics(page=page, size=size))), op, full)
        if op == "users":
            return _shape(paged(run(lambda: c.list_users(
                email=email, page=page, size=size))), op, full)
        if op == "teams":
            return _shape(paged(run(lambda: c.list_teams(
                name=name, page=page, size=size))), op, full)
        if op == "crm_templates":
            return _shape(paged(run(lambda: c.list_crm_filling_templates(
                status=status, page=page, size=size))), op, full)
        if op == "user":
            need(op, user_id=user_id)
            return run(lambda: c.get_user(user_id), "no member with this id")
        if op in ("team", "team_members"):
            need(op, team_id=team_id)
            if op == "team":
                return run(lambda: c.get_team(team_id), "no team with this id")
            return _shape(paged(run(lambda: c.list_team_members(
                team_id, page=page, size=size), "no team with this id")), op, full)
        need(op, template_uuid=template_uuid)
        if op == "crm_template":
            return run(lambda: c.get_crm_filling_template(template_uuid),
                       "no CRM filling template with this uuid")
        return _shape(paged(run(lambda: c.list_crm_filling_template_fields(
            template_uuid, page=page, size=size),
            "no CRM filling template with this uuid")), op, full)

    @mcp.tool()
    def modjo_ask(
        scope_type: Literal["call", "deal", "account"],
        scope_id: Union[int, str],
        answer_schema: Dict[str, Any],
        question: Optional[str] = None,
        schema_name: str = "answer",
        schema_description: Optional[str] = None,
        strict: Optional[bool] = None,
    ) -> Any:
        """Modjo — ask Modjo's AI about ONE call, deal or account; it answers in
        the JSON shape of `answer_schema` (`{id, scope, output}`, any field of
        `output` may be null). Synchronous: 30 s to 2 min, up to 5 min. Never
        retried, since each attempt runs the AI again. Refused when the feature is
        not enabled for the workspace (403), or when the call is not analysed yet
        (409).

        The schema accepts object, array, string, number, integer, boolean and
        scalar enums only (no pattern, minLength, $ref, oneOf), 100 properties
        and 5 levels at most.

        Args:
            scope_type: call | deal | account.
            scope_id: its id (a call also takes its UUID).
            answer_schema: JSON Schema of the answer, e.g. {"type": "object",
                "properties": {"budget_mentioned": {"type": "boolean"}}}.
            question: the instructions, 1-4,000 characters.
            schema_name: name of the answer format.
            schema_description: what the answer is, 1,000 characters at most.
            strict: whether Modjo enforces the schema strictly.
        """
        if isinstance(scope_id, str) and scope_type != "call":
            if not scope_id.isdigit():
                raise _bad(f"`scope_id` of a {scope_type} is its numeric id.")
            scope_id = int(scope_id)
        return run(lambda: _client().ask_agent(
            scope_type, scope_id, instructions=question, schema=answer_schema,
            schema_name=schema_name, schema_description=schema_description,
            strict=strict), f"no {scope_type} with this id in the workspace")

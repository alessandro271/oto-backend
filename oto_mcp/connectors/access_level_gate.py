"""Where an access level BITES — call guard `access_level_denied`, hidden tools, served ops.

`connectors.access_levels` says what a level covers; this module applies it, and only
when the level in force RESTRICTS (`full` filters nothing — not one query beyond the
reading of the setting, not one byte of schema changed):

- **visibility** (`hidden_tools`, a layer of `session_visibility`): a tool none of whose
  scopes or ops the level covers leaves the toolbox. Display governance, fail-open like
  the other layers — the guard below is the barrier;
- **served schema** (`restricted_ops` + `restrict_schema`, applied per caller by the
  context middleware's `on_list_tools`): the ops of a multiplexed tool that the level
  does not cover leave the `op` enum;
- **call guard** (`require_covered`), **fail-closed**, at the two places a tool is run:
  the context middleware (direct call) and `tools/meta.executer_cible` (`oto_call` and
  the recipes), right after the activation guard — same seam, same context: the org and
  the team the call resolves under, axes and the run's org included. It receives the
  arguments, so it judges the `op`; a call without `op` is judged as the tool's default.
  An undeclared tool or op of a leveled connector is refused, never let through.

Without a sub (local stdio) nothing is guarded, like activation: only the multi-user
surface is targeted.
"""
from __future__ import annotations

import copy
from typing import Awaitable, Callable, Optional

from mcp.types import INVALID_PARAMS, ErrorData
from starlette.concurrency import run_in_threadpool

from .. import access, call_axes
from ..mcp_errors import McpError
from . import access_levels

CODE = "access_level_denied"


def _context(sub: str) -> tuple[Optional[int], Optional[int]]:
    return access.current_org(sub), access.current_group(sub)


# --- the call guard --------------------------------------------------------------

def refusal(decl: access_levels.AccessLevels, level: str, source: str, tool: str,
            op: Optional[str], org: Optional[int], group: Optional[int]) -> ErrorData:
    """The named refusal: the level, the tool and the op, what they require, who sets it."""
    exige = decl.required(tool, op)
    cible = f"`{tool}` op `{op}`" if op is not None else f"`{tool}`"
    par = (f"your team #{group}" if source == "group" else f"organization #{org}")
    besoin = (f"it requires {', '.join(exige)}" if exige
              else "nothing is declared for it at this level")
    if source == "group":
        geste = (f"a team lead lifts it (oto_connector_access_level(op='clear', "
                 f"scope='group', group_id={group}, name='{decl.connector}'))")
    else:
        geste = (f"an org admin widens it (oto_connector_access_level(op='set', "
                 f"scope='org', org_id={org}, name='{decl.connector}', level='full'))")
    return ErrorData(
        code=INVALID_PARAMS,
        message=(f"Refusal `{CODE}`: the access level `{level}` set by {par} for "
                 f"`{decl.connector}` does not cover {cible} — {besoin}. The call is "
                 f"served neither directly nor through oto_call. To allow it, {geste}; "
                 f"a wider level then needs a new consent (a token obtained for a "
                 f"narrower level lacks its scopes)."),
        data={"code": CODE, "retryable": False, "connector": decl.connector,
              "level": level, "tool": tool, "op": op,
              "required_scopes": list(exige or ()), "scope": source,
              "org_id": org, "group_id": group})


def _verdict(tool: str, args: dict, default_op: Optional[str]) -> Optional[ErrorData]:
    """Sync (threadpool): the refusal for this call, or None."""
    decl = access_levels.for_tool(tool)
    if decl is None:
        return None
    sub = call_axes.current_user_sub_from_token()
    if not sub:
        return None
    org, group = _context(sub)
    level, source = access_levels.resolve(decl.connector, org, group)
    if not access_levels.restricts(level):
        return None
    op = None
    if decl.is_op_tool(tool):
        op = args.get(access_levels.OP_PARAM, default_op)
    if decl.covers(level, tool, op):
        return None
    return refusal(decl, level, source, tool, op, org, group)


def default_op(parameters: Optional[dict]) -> Optional[str]:
    """The default of the `op` parameter in a served input schema, or None."""
    prop = ((parameters or {}).get("properties") or {}).get(access_levels.OP_PARAM) or {}
    return prop.get("default")


async def require_covered(tool: str, args: dict,
                          tool_schema: Callable[[], Awaitable[Optional[dict]]]) -> None:
    """Raises `access_level_denied` if the level in force does not cover this call.
    `tool_schema` gives the tool's input schema — awaited only for a multiplexed tool
    called WITHOUT `op` under a restricting level (its default op is then judged)."""
    decl = access_levels.for_tool(tool)
    if decl is None:
        return
    defaut = None
    if decl.is_op_tool(tool) and access_levels.OP_PARAM not in args:
        defaut = default_op(await tool_schema())
    refus = await run_in_threadpool(_verdict, tool, args, defaut)
    if refus is not None:
        raise McpError(refus)


# --- visibility and served schema -------------------------------------------------

def _restricting(org: Optional[int], group: Optional[int]) -> dict:
    """`{connector: level}` for every leveled connector whose level restricts."""
    if not access_levels.DECLARED:
        return {}
    valeurs = access_levels.settings(org, group)
    out = {}
    for nom in access_levels.DECLARED:
        level = access_levels.resolve(nom, org, group, valeurs)[0]
        if access_levels.restricts(level):
            out[nom] = level
    return out


def hidden_tools(all_names: set[str], org: Optional[int],
                 group: Optional[int]) -> set[str]:
    """The tools of `all_names` that the level in force covers in NOTHING: a non-op tool
    whose scopes it lacks, a multiplexed tool none of whose ops it covers, a tool of a
    leveled connector that declares nothing."""
    niveaux = _restricting(org, group)
    if not niveaux:
        return set()
    caches = set()
    for n in all_names:
        decl = access_levels.for_tool(n)
        if decl is None or decl.connector not in niveaux:
            continue
        level = niveaux[decl.connector]
        if decl.is_op_tool(n):
            if not decl.covered_ops(level, n):
                caches.add(n)
        elif not decl.covers(level, n):
            caches.add(n)
    return caches


def restricted_ops(sub: Optional[str]) -> dict[str, frozenset[str]]:
    """`{tool: covered ops}` for the multiplexed tools whose level restricts, under the
    caller's org and active team. Sync (threadpool). Empty without a sub."""
    if not sub:
        return {}
    niveaux = _restricting(*_context(sub))
    return {tool: decl.covered_ops(niveaux[nom], tool)
            for nom, decl in access_levels.DECLARED.items() if nom in niveaux
            for tool in decl.tools if decl.is_op_tool(tool)}


def restrict_schema(parameters: Optional[dict], ops: frozenset[str]) -> dict:
    """A copy of the input schema whose `op` enum keeps only `ops`. A default outside
    `ops` is dropped and `op` becomes required: a call without it would otherwise run
    an op the level refuses."""
    params = copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
    prop = (params.get("properties") or {}).get(access_levels.OP_PARAM)
    if not isinstance(prop, dict):
        return params
    for branche in [prop, *(prop.get("anyOf") or [])]:
        if isinstance(branche, dict) and isinstance(branche.get("enum"), list):
            branche["enum"] = [v for v in branche["enum"] if v in ops]
    if "default" in prop and prop["default"] not in ops:
        del prop["default"]
        requis = params.setdefault("required", [])
        if access_levels.OP_PARAM not in requis:
            requis.append(access_levels.OP_PARAM)
    return params

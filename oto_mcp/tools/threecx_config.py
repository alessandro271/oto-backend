"""3CX configuration, READ — `threecx_config`: what the phone system exposes and how it
is set up (extensions, queues, ring groups, IVRs, inbound rules, office hours,
forwarding, settings…).

Generic over the Configuration API (XAPI, OData v4): nothing is listed by hand, the
PBX's own `$metadata` says which resources, properties and operations exist
(`oto.tools.threecx.Schema`), and every path is checked against it before the
request. Every credential read is masked (`schema.mask`); an operation that hands
one out is refused by the client.

Writes are `threecx_act` (convention `<ns>_act`, 17/10/2026): an MCP client
authorizes tools, so "always allow" on this reader never changes the PBX. What an
account can read is bounded by its 3CX role; the configuration needs an admin.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastmcp import FastMCP

from .lecture import LECTURE
from . import threecx
from .threecx import _bad, run

_OPS = ("catalog", "schema", "read", "function")
# The arguments each op uses; any other one provided is an error of intent.
_ARGS = {
    "catalog": set(),
    "schema": {"resource"},
    "read": {"resource", "key", "navigation", "filter", "select", "expand", "orderby",
             "top", "skip"},
    "function": {"resource", "key", "operation", "arguments"},
}
_TOP = 50


def _refuse_ignored(op: str, provided: dict) -> None:
    extra = sorted(n for n, v in provided.items() if v is not None and n not in _ARGS[op])
    if extra:
        raise _bad(f"op='{op}' does not use {', '.join(extra)}.")


def register(mcp: FastMCP) -> None:
    from oto.tools.threecx.schema import SECRET_OPERATION, mask

    @mcp.tool(annotations=LECTURE)
    def threecx_config(
        op: Literal["catalog", "schema", "read", "function"],
        resource: Optional[str] = None,
        key: Optional[Union[int, str, dict]] = None,
        navigation: Optional[str] = None,
        operation: Optional[str] = None,
        arguments: Optional[dict] = None,
        filter: Optional[str] = None,
        select: Optional[list] = None,
        expand: Optional[str] = None,
        orderby: Optional[str] = None,
        top: Optional[int] = None,
        skip: Optional[int] = None,
    ) -> dict:
        """READS the configuration of the 3CX phone system: users and extensions,
        queues and their agents, ring groups, IVRs, inbound rules (which number
        rings where, in and out of office hours), office hours, forwarding
        profiles, trunks, settings. Changing it is `threecx_act`.

        Work from the PBX's own schema, never from memory:
        - **"catalog"** → every resource name: `collections` (Users, Queues,
          RingGroups, Receptionists = IVRs, InboundRules, Groups = departments…)
          and `settings` (singletons such as VoicemailSettings).
        - **"schema"** (`resource`) → its key, properties (enum values
          included), navigations and operations.
        - **"read"** (`resource`, optional `key`, `navigation`) → a page of a
          collection `{value, next_skip}` (continue with `skip=next_skip` until
          null), one entity, a navigation of it (a queue's `Agents`), or a
          setting. OData `filter` (e.g. `Number eq '8003'`), `select`
          (property names — rows are large, select what you need), `expand`,
          `orderby`.
        - **"function"** (`operation`, optional `resource`/`key`, `arguments`) →
          a read-only operation listed by "schema" (`kind: function`), e.g.
          a report. Actions are `threecx_act(op="call")`.

        Passwords, SIP secrets, provisioning links, API and licence keys come
        back as `***`. The account's 3CX role bounds what is visible.

        Args:
            op: catalog | schema | read | function.
            resource: a name from "catalog", e.g. InboundRules.
            key: the entity's key — an `Id` number, a string key, or an object
                for a composite key; as "schema" gives it.
            navigation: a navigation of that entity, e.g. Agents.
            operation: op="function" — the function's name.
            arguments: op="function" — its parameters, by name.
            filter / select / expand / orderby: op="read", OData options.
            top: op="read" on a collection — rows per page, 1 to 500 (default 50).
            skip: op="read" — the `next_skip` of the previous page.
        """
        provided = dict(resource=resource, key=key, navigation=navigation,
                        operation=operation, arguments=arguments, filter=filter,
                        select=select, expand=expand, orderby=orderby, top=top, skip=skip)
        _refuse_ignored(op, provided)
        c = threecx.client()
        if op == "catalog":
            s = run(c.schema)
            return {"collections": sorted(s.sets), "settings": sorted(s.singletons),
                    "root_operations": sorted(o.name for o in s.operations
                                              if o.bound_to is None
                                              and not SECRET_OPERATION.search(o.name))}
        if not resource and op != "function":
            raise _bad(f"op='{op}' needs `resource` — op='catalog' lists them.")
        if op == "schema":
            return run(lambda: c.schema().describe(resource))
        if op == "read":
            return mask(run(lambda: c.read(
                resource, key=key, navigation=navigation, filter=filter, select=select,
                expand=expand, orderby=orderby, top=_TOP if top is None else top,
                skip=skip or 0)))
        if not operation:
            raise _bad("op='function' needs `operation` — op='schema' lists them.")

        def call() -> Any:
            s = c.schema()
            on_entity = key is not None or (resource is not None and s.is_singleton(resource))
            if s.operation(operation, resource, on_entity=on_entity).kind != "function":
                raise ValueError(f"{operation} is an action, it acts: "
                                 f"threecx_act(op='call', operation='{operation}').")
            return c.invoke(operation, resource=resource, key=key, arguments=arguments)

        return {"value": mask(run(call))}

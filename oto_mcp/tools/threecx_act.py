"""3CX configuration, WRITE — `threecx_act`: every change to the phone system, in its
own tool (convention `<ns>_act`, 17/10/2026: an MCP client authorizes tools, and
"always allow" on the reader must never reroute a company's calls).

Generic over the Configuration API, like `threecx_config`: the PBX's `$metadata`
decides which resources, properties and actions exist, and the client checks the
path and the body against it before any request.

**A preview first, always by default** (`dry_run=True`): an update shows each
property's current and requested value, a delete shows the entity, a create or
an action shows what would be sent — nothing is written. `dry_run=False` writes,
then an update READS BACK what the PBX holds and says, property by property,
whether it matches: an acknowledgement is not a write.

A change takes effect on live calls at once: there is no draft on the PBX side.
Credentials are never written (refused by the client); an action that hands one
out is refused too.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastmcp import FastMCP

from .lecture import ENVOI
from . import threecx
from .threecx import _bad, run

# The arguments each op uses; any other one provided is an error of intent.
_ARGS = {
    "create": {"resource", "body"},
    "update": {"resource", "key", "body"},
    "delete": {"resource", "key"},
    "call": {"resource", "key", "operation", "arguments"},
}
_NOTE = ("Preview: nothing is written. Show it to the person, then call again with "
         "dry_run=False.")


def _refuse_ignored(op: str, provided: dict) -> None:
    extra = sorted(n for n, v in provided.items() if v is not None and n not in _ARGS[op])
    if extra:
        raise _bad(f"op='{op}' does not use {', '.join(extra)}.")


def _contains(actual: Any, wanted: Any) -> bool:
    """`wanted` is held by `actual`: equal values, or every property of a wanted
    object held by the actual one (the PBX completes a destination with its name)."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            _contains(actual.get(k), v) for k, v in wanted.items())
    if isinstance(wanted, list):
        return (isinstance(actual, list) and len(actual) == len(wanted)
                and all(_contains(a, w) for a, w in zip(actual, wanted)))
    return actual == wanted


def register(mcp: FastMCP) -> None:
    from oto.tools.threecx.schema import mask

    @mcp.tool(annotations=ENVOI)
    def threecx_act(
        op: Literal["create", "update", "delete", "call"],
        resource: Optional[str] = None,
        key: Optional[Union[int, str, dict]] = None,
        body: Optional[dict] = None,
        operation: Optional[str] = None,
        arguments: Optional[dict] = None,
        dry_run: bool = True,
    ) -> dict:
        """CHANGES the configuration of the 3CX phone system — live: a change
        reroutes real calls at once. Read first with `threecx_config` (op="schema"
        for the properties, op="read" for the current values).

        By default nothing is written (`dry_run=True`): you get a preview to show
        the person. Write with `dry_run=False` only after they confirm.

        - **"update"** (`resource`, `key` — none for a setting —, `body`: only
          the properties to change) → preview: before/after per property;
          written: the values the PBX holds afterwards and `verified`. E.g.
          reroute a number: `resource="InboundRules"`, `key=<Id>`,
          `body={"OfficeHoursDestination": {"To": "Queue", "Number": "8003"}}`
          (`To`: Extension | Queue | RingGroup | IVR | VoiceMail | External…).
          A navigation (a queue's `Agents`) is a list in the body: send the
          WHOLE list, read it first.
        - **"create"** (`resource`, `body`) → the new entity.
        - **"delete"** (`resource`, `key`) → preview: the entity.
        - **"call"** (`operation`, optional `resource`/`key`, `arguments`) → an
          action listed by `threecx_config(op="schema")` (`kind: action`).

        Passwords, SIP secrets, provisioning links, API and licence keys are
        never written here: the 3CX admin console sets them. The account needs
        a 3CX admin role.

        Args:
            op: create | update | delete | call — required.
            resource: a name from threecx_config(op="catalog").
            key: the entity's key, as threecx_config(op="schema") gives it.
            body: create/update — property → value.
            operation: call — the action's name.
            arguments: call — its parameters, by name.
            dry_run: True (default) previews, False writes.
        """
        provided = dict(resource=resource, key=key, body=body, operation=operation,
                        arguments=arguments)
        _refuse_ignored(op, provided)
        if op != "call" and not resource:
            raise _bad(f"op='{op}' needs `resource` — threecx_config(op='catalog').")
        c = threecx.client()

        if op == "update":
            if not body:
                raise _bad("op='update' needs `body`: the properties to change.")
            if dry_run:
                def preview() -> dict:
                    c.schema().check_body(resource, body)
                    if key is None and not c.schema().is_singleton(resource):
                        raise ValueError(f"updating {resource} needs the key of the entity.")
                    current = c.read(resource, key=key)["value"] or {}
                    changes = {k: {"before": mask(current.get(k)), "after": mask(v)}
                               for k, v in body.items() if not _contains(current.get(k), v)}
                    return {"dry_run": True, "resource": resource, "key": key,
                            "changes": changes,
                            "unchanged": sorted(set(body) - set(changes)), "note": _NOTE}
                return run(preview)

            def write() -> dict:
                c.update(resource, body, key=key)
                after = c.read(resource, key=key)["value"] or {}
                ecarts = sorted(k for k, v in body.items() if not _contains(after.get(k), v))
                return {"written": True, "resource": resource, "key": key,
                        "now": mask({k: after.get(k) for k in body}),
                        "verified": not ecarts,
                        **({"not_held": ecarts} if ecarts else {})}
            return run(write)

        if op == "create":
            if not body:
                raise _bad("op='create' needs `body`.")
            if dry_run:
                def check() -> dict:
                    s = c.schema()
                    if s.is_singleton(resource):
                        raise ValueError(f"{resource} is a setting: update it.")
                    s.check_body(resource, body)
                    return {"dry_run": True, "would_create": resource, "body": mask(body),
                            "note": _NOTE}
                return run(check)
            return {"written": True, "created": mask(run(lambda: c.create(resource, body)))}

        if op == "delete":
            if key is None:
                raise _bad("op='delete' needs `key`.")
            if dry_run:
                return {"dry_run": True, "would_delete": resource, "key": key,
                        "entity": mask(run(lambda: c.read(resource, key=key))["value"]),
                        "note": _NOTE}
            run(lambda: c.delete(resource, key=key))
            return {"written": True, "deleted": resource, "key": key}

        if not operation:
            raise _bad("op='call' needs `operation` — threecx_config(op='schema') lists them.")

        def action():
            s = c.schema()
            on_entity = key is not None or (resource is not None and s.is_singleton(resource))
            found = s.operation(operation, resource, on_entity=on_entity)
            if found.kind != "action":
                raise ValueError(f"{operation} is a function, it only reads: "
                                 f"threecx_config(op='function', operation='{operation}').")
            return found

        found = run(action)
        if dry_run:
            return {"dry_run": True, "would_call": found.name, "resource": resource,
                    "key": key, "arguments": mask(arguments or {}), "note": _NOTE}
        result = run(lambda: c.invoke(operation, resource=resource, key=key,
                                      arguments=arguments))
        return {"written": True, "called": found.name, "result": mask(result)}

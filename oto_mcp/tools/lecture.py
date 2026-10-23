"""A tool that only READS at its provider DECLARES it, on its decorator:
`@mcp.tool(annotations=LECTURE)`.

The declaration is the protocol's standard annotation (`readOnlyHint`): the MCP client sees it
too. It says "this call changes nothing at the third party" — not "it is free"
(an Apollo or AI Ark search bills its credits), nor "it does not run a
model": a model connector (jev, lighton) reads, but a recipe does not call it
(`recipes/contrat.NAMESPACES_A_MODELE`).

**What reads it**: `oto_recipe`. A "pull" recipe calls ONLY tools declared
here — an undeclared tool is refused, never presumed a reader: the default is refusal.
A tool multiplexed by `op` is only declared if ALL its ops read.

**Its counterpart, `ENVOI`**: a tool that acts AT A THIRD PARTY or in the person's name
(a message, an invitation, a publication) declares it too. An MCP client authorizes
TOOLS, not operations: "always allow" on a reader must never allow a send — which is
why such a send lives in its own tool, never as an `op` of a reader (oto-backend#1189).
Since 21/10/2026 no reader serves an acting op any more, even in transition: the old op
is refused, naming its new call (`outils_retires.OPS_DEPLACEES`).
`destructiveHint`/`openWorldHint` are the protocol's defaults for a tool that is not
read-only; they are WRITTEN here so that the client reads them, not infers them.
"""
from __future__ import annotations

from mcp.types import ToolAnnotations

LECTURE = ToolAnnotations(readOnlyHint=True)
ENVOI = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

# --- a connector's TWO tools: `<ns>` reads or simulates, `<ns>_act` acts (17/10/2026) ---
# Alexis's decision, for security: one pair per connector, never a send or a delete as an
# `op` of the tool that reads. `<ns>` declares `LECTURE` when it only reads, `SIMULATION`
# when it also prepares something nobody sees and nothing destroys (a draft): it writes in
# the person's own space, so it is not read-only, but it neither reaches a third party nor
# deletes. `<ns>_act` declares `ACTE`: it sends, deletes or changes — the tool an MCP client
# asks to confirm. Guarded by `tests/test_convention_act.py`; the old names are refused by
# `outils_retires.RENOMMAGES`.
SIMULATION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
ACTE = ENVOI


def documente(texte: str):
    """Give a tool function its docstring from a COMPOSED text — when two tools of a pair
    share a passage (the composition arguments of `<ns>` and `<ns>_act`), it is written
    once. A docstring, not `description=`: FastMCP parses its `Args:` into the parameter
    descriptions, as for any other tool."""
    def poser(fn):
        fn.__doc__ = texte
        return fn
    return poser


def en_lecture(tool) -> bool:
    """Has the tool (FastMCP object) declared itself read-only?"""
    annotations = getattr(tool, "annotations", None)
    return getattr(annotations, "readOnlyHint", None) is True

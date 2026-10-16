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
Until `deprecations.RETRAIT_OPS_ENVOI`, a reader that still serves its old send ops is
itself declared `ENVOI`: it sends (`OpsDepreciees`, `en_transition` below — shared by the
Unipile readers, #1189, and by `chat_message` / `teams_message`, #1231).
`destructiveHint`/`openWorldHint` are the protocol's defaults for a tool that is not
read-only; they are WRITTEN here so that the client reads them, not infers them.
"""
from __future__ import annotations

import inspect
from typing import NamedTuple, Optional

from mcp.types import ToolAnnotations

from .. import deprecations

LECTURE = ToolAnnotations(readOnlyHint=True)
ENVOI = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

# --- a connector's TWO tools: `<ns>` reads or simulates, `<ns>_act` acts (17/10/2026) ---
# Alexis's decision, for security: one pair per connector, never a send or a delete as an
# `op` of the tool that reads. `<ns>` declares `LECTURE` when it only reads, `SIMULATION`
# when it also prepares something nobody sees and nothing destroys (a draft): it writes in
# the person's own space, so it is not read-only, but it neither reaches a third party nor
# deletes. `<ns>_act` declares `ACTE`: it sends, deletes or changes — the tool an MCP client
# asks to confirm. Guarded by `tests/test_convention_act.py`; the old names are refused by
# `outils_retires.RENOMMAGES_ACT`.
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


# --- the TRANSITION of a reader's old send ops (oto-backend#1189, #1231) --------------
# Until `deprecations.RETRAIT_OPS_ENVOI` (10/12/2026 included, Alexis's decisions of
# 12/10 and 13/10/2026), the old op of a reader still works. It is served BY the
# writer's own function — same refusals, before any credential — and its response
# carries `deprecation_warning`. While a reader can send, it stays declared `ENVOI`,
# never `LECTURE`: a client that took it for a reader would send without asking. At the
# retirement, the ops leave the readers' schema, the readers become `LECTURE`, and a call
# that still sends an old op is refused by naming its writer (`op_moved`).


class OpsDepreciees(NamedTuple):
    """The send ops a reader still serves until the retirement, and their writer."""
    outil: str           # the reader
    cible: str           # the writer that serves them
    ops: tuple           # the deprecated ops, in the reader's `op`
    garde_op: bool = True  # the writer takes `op` — `whatsapp_send`, `chat_send` do not

    def avis(self) -> str:
        """The ONE line of the reader's description, at its head: a long description is
        often truncated, and the head is what the model reads to pick its tool."""
        return (f"⚠️ op={'/'.join(self.ops)} est déprécié, utiliser `{self.cible}` ; "
                f"retrait le {deprecations.date_retrait_ops_envoi()}.")

    def _avec_avis(self, op: str, out: dict) -> dict:
        appel = (f'`{self.cible}(op="{op}", …)` avec les mêmes arguments' if self.garde_op
                 else f"`{self.cible}(…)` avec les mêmes arguments, sans `op`")
        return {**out, "deprecation_warning": (
            f'Déprécié : `{self.outil}(op="{op}")` est retiré le '
            f"{deprecations.date_retrait_ops_envoi()}. L'appel a été servi par "
            f"`{self.cible}` ; appelle désormais {appel}.")}

    def servir(self, op: str, ecrivain, /, **arguments) -> dict:
        """`op` served by the writer's function itself — never a copy of it — and SAID
        in the response, with the call to make from now on."""
        return self._avec_avis(op, ecrivain(**arguments))

    async def servir_async(self, op: str, ecrivain, /, **arguments) -> dict:
        """`servir`, for a writer that is a coroutine function (`chat_send`)."""
        return self._avec_avis(op, await ecrivain(**arguments))


def en_transition(mcp, depreciees: OpsDepreciees, description: Optional[str] = None):
    """Mount a reader that still serves send ops: declared `ENVOI` (it sends), the
    deprecation line at the head of its description (its docstring when not given)."""
    def monter(fn):
        corps = description if description is not None else inspect.getdoc(fn)
        return mcp.tool(name=depreciees.outil, annotations=ENVOI,
                        description=f"{depreciees.avis()}\n\n{corps}")(fn)
    return monter

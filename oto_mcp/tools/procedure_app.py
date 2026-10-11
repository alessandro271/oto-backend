"""Procedure — the rendered GRAPH (`oto_procedure_app`): a procedure's drawing, drawn
in the conversation as the partner front's process page draws it. READ-ONLY.

**Per instance, by configuration.** The drawing is optional in the core (18/09/2026: a
partner's rendering need is not a core rule), and its grammar lives in ONE place, the
partner's front (`procedure_diagram.py` refuses a Python twin). So this module draws
nothing itself: its ui:// page is a shell that loads the front's own bundle, and the
tool only ships the drawing. `OTO_PROCEDURE_APP_BUNDLE` (the bundle's URL, e.g.
`https://app.example.com/mcp-app/process-graph.js`, its stylesheet next to it as
`.css`) is both the address and the switch: unset, nothing registers — an instance
without such a front serves no app pointing at nobody. No tenant axis (frozen,
`docs/tenants.md`): one instance, one front.

Not a Prefab card (`docs_app`, `data_review_app`): Prefab renders its own component
tree, and the drawing needs the front's parser and canvas. A plain MCP Apps resource
(SEP-1865), whose CSP admits the bundle's origin and the connector logos.

**Two channels** (cf. `docs_app`, signal #1083): `structuredContent` feeds the view
(the drawing + the connectors it may name, for their logos) and is kept by
`un_seul_canal` because the tool declares `_meta.ui.resourceUri`; the MODEL reads one
line of text pointing at `oto_procedure op=get` for the steps.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

URI = "ui://oto/procedure-graph.html"

# Logos come from the registry (logo.dev for almost all); a host that blocks one
# shows the front's fallback mark instead — never a broken image.
_LOGO_DOMAINS = ("https://img.logo.dev",)

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="{css}">
</head>
<body>
<script src="{js}"></script>
</body>
</html>
"""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _connectors_named(trace: str, org_id: int | None) -> list[dict]:
    """The registry connectors the drawing may name — a SUPERSET of what the front's
    matching will keep (a namespace as a token, or the label as a word), so the view
    re-runs the real matching on a roster of a few entries instead of 600 KB. Through
    the org's effective overlay, like every surface that serves the catalogue (#732)."""
    from .. import providers
    from ..connectors import cardinality
    texte = trace.lower()
    mots = set(re.findall(r"[a-z0-9]+", texte))
    retenus = []
    for c in cardinality.overlay_for_org(providers.public_catalog(), org_id):
        namespaces = c.get("namespaces") or []
        label = (c.get("label") or "").lower()
        if any(ns in mots or f"{ns}_" in texte for ns in namespaces) or (
                label and label in texte) or c["name"] in mots:
            retenus.append({
                "name": c["name"], "label": c.get("label") or c["name"],
                "category": c.get("category") or "", "logo_url": c.get("logo_url"),
                "namespaces": namespaces, "state": "active",
            })
    return retenus


def _lire(sub: str, slug: str) -> tuple[dict, str] | None:
    """The procedure `slug` and its scope (`org`|`group`), in the order
    `oto_procedure(op='get')` serves it: the active org first, the active team as a
    complement (`guide_run.version_de_procedure`)."""
    from .. import access, org_store
    org_id = access.current_org(sub)
    if org_id is not None:
        row = org_store.get_instruction("org", org_id, slug)
        if row:
            return row, "org"
    gid = access.current_group(sub)
    if gid is not None:
        row = org_store.get_instruction("group", gid, slug)
        if row:
            return row, "group"
    return None


def _aux_noms_du_produit(sub: str, texte: str) -> str:
    """Les outils cités, au nom que CE compte voit dans sa liste (`guides._get`)."""
    from .. import tool_alias
    prefix = tool_alias.prefix_for(sub)
    return tool_alias.rewrite_prose(texte, prefix) if prefix else texte


def _a_dessiner(slug: str, scope: str, version) -> str:
    """No drawing yet: the agent draws one in the grammar the front turns into its
    figure — never Mermaid, never a tagged fence (the front only draws an UNTAGGED
    block, `procedure_diagram` header) — writes it back, and calls again."""
    return (
        f"REQUIRED NEXT STEP — do NOT answer the user yet. This process has no drawing, "
        f"so the user sees nothing: draw it, write it, and show it, in this order. "
        f"1) oto_guide op=read slug=procedure-flowchart — the drawing grammar; follow it "
        f"exactly. 2) oto_procedure op=get slug={slug} — the body. 3) Draw the flow from "
        f"ITS steps: the trigger, each step in a box naming the tools it uses, the checks "
        f"as decisions, the rows that leave as exits — box-drawing characters (┌ ─ ┐ │ └ "
        f"┘ ┬ ▼ ├ ▶ ▪) in ONE untagged ``` fence. Never Mermaid, never ```text: those are "
        f"not drawn as the figure. 4) Put it where the guide says (after the intro or the "
        f"summary table, before the first phase heading), change NOTHING else, and write "
        f"it: oto_procedure op=set slug={slug} scope={scope} expected_version={version} "
        f"body_md=<the whole body>. A non-null `diagram_warning` in the answer means it "
        f"will not render: fix the named line and set again. 5) Call oto_procedure_app "
        f"slug={slug} again — the user sees the graph. ONLY THEN answer. If the write is "
        f"refused, tell the user you could not add the drawing and summarise the steps.")


def register(mcp: FastMCP) -> None:
    from ..procedure_diagram import bundle_du_graphe
    bundle = bundle_du_graphe()
    if bundle is None:
        return
    try:
        from fastmcp.apps import AppConfig, ResourceCSP, UI_MIME_TYPE
    # noqa: SILENT — extra `apps` absent ⇒ no app, oto_procedure is enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    page = _SHELL.format(js=html.escape(bundle, quote=True),
                         css=html.escape(bundle[:-3] + ".css", quote=True))

    @mcp.resource(URI, name="procedure_graph", mime_type=UI_MIME_TYPE,
                  app=AppConfig(csp=ResourceCSP(resource_domains=[_origin(bundle), *_LOGO_DOMAINS]),
                                prefers_border=True))
    def procedure_graph_page() -> str:
        return page

    @mcp.tool(app=AppConfig(resource_uri=URI))
    def oto_procedure_app(slug: str) -> ToolResult:
        """SHOW a process to the user: draws the procedure's process graph INLINE in the
        conversation (MCP App) — trigger, steps, checks and the connectors each step uses.

        ALWAYS call it, FIRST, whenever the user asks about a process or procedure:
        to see it, understand it, explain it, review it, or after you create or change
        one. Then answer in text — the graph replaces no explanation. It gives YOU only
        the title: the steps are in `oto_procedure op=get`. If the process has no
        drawing yet, the answer tells you how to draw one: do it, then call this again.

        Args:
            slug: the procedure's slug (cf. `oto_procedure op=list`).
        """
        from .. import access
        from ..procedure_diagram import le_trace
        sub = access.current_user_sub_or_raise()
        lu = _lire(sub, slug)
        if lu is None:
            message = _aux_noms_du_produit(
                sub, f"No procedure `{slug}` in your active org or team — list them with oto_procedure op=list.")
            return ToolResult(content=[TextContent(type="text", text=message)],
                              structured_content={"slug": slug, "message": message})
        row, scope = lu
        title = row.get("title") or slug
        version = row.get("version")
        trace = le_trace(row.get("body_md") or "")
        rendu = {"slug": slug, "title": title, "version": version, "drawing": trace,
                 "connectors": _connectors_named(trace, access.current_org(sub)) if trace else []}
        if trace:
            texte = (f"{title} (`{slug}`, v{version}) — the graph is drawn for the user. "
                     f"Its steps: oto_procedure op=get slug={slug}.")
        else:
            texte = f"{title} (`{slug}`, v{version}). " + _a_dessiner(slug, scope, version)
        return ToolResult(content=[TextContent(type="text", text=_aux_noms_du_produit(sub, texte))],
                          structured_content=rendu)

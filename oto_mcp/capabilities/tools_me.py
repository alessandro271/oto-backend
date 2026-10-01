"""La TOOLBOX du membre — ce que le dashboard montre et pilote des outils exposés
à l'appelant.

Six routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `GET    /api/me/tools`               → tous les tools + leur état (activé/désactivé)
- `GET    /api/me/tools/registry`      → registre résolu (ADR 0014), matière des `<tool:slug>`
- `POST   /api/me/tools/{name}`        → DÉSACTIVE (visibilité-only, ADR 0031)
- `DELETE /api/me/tools/{name}`        → RÉACTIVE
- `GET    /api/me/tools/{name}/detail` → fiche : description + schémas + connecteur
- `POST   /api/me/tools/{name}/call`   → exécute un outil TESTABLE sous l'identité de l'appelant

⚠️ **POST désactive, DELETE réactive.** L'inversion est contre-intuitive et elle est
HISTORIQUE : le chemin nomme la ligne de denylist, pas le tool — poser la ligne
(POST) masque, la retirer (DELETE) démasque. Le dashboard s'y branche depuis toujours ;
la migration ne l'a pas « corrigée », elle l'a figée par test.

⚠️ **Les six migrent EN BLOC, et c'est une contrainte de routage, pas de confort.**
`{name}` capture un segment, donc `…/tools/registry` DOIT précéder `…/tools/{name}`.
Les routes de capacité sont montées à la FIN de `make_routes` : migrer `registry` sans
`{name}` (ou l'inverse) placerait le générique avant le spécifique et `registry`
serait servi comme un nom d'outil. L'ordre de déclaration ci-dessous EST cet ordre.

**Deux faces depuis #429.** La toolbox avait un miroir MCP écrit à la main
(`tools/meta.py`) : deux implémentations du même geste, deux autz à tenir en phase.
Masquer/démasquer est désormais UNE capacité par geste, servie aux deux faces
(`oto_disable_tool` / `oto_enable_tool`). La liste, elle, reste DEUX capacités sur un
noyau partagé, parce que les deux faces ne posent pas la même question :
`GET /api/me/tools` peint la grille de gouvernance (liste complète, `{name, enabled,
protected}`), `oto_list_my_tools` sert l'agent qui CHERCHE un outil (catalogue avec
l'état de chaque outil, recherche lexicale, projection). Les fondre aurait cassé l'une
des deux surfaces ; le noyau commun est `tools/catalogue.py`.

`mcp_instance` était passé de `make_routes` jusqu'au handler ; il est désormais résolu
à l'APPEL via `tool_registry.bound_instance()` (le singleton lié au boot par
`server._build_mcp`). C'est le MÊME objet : `server.py` appelle `_build_mcp` — donc
`tool_registry.bind` — puis `api.routes.make_routes(verifier, mcp_instance=mcp)` avec
cette instance-là. Résoudre à l'appel plutôt qu'au montage est strictement plus juste
(une re-liaison ultérieure serait suivie), et c'est déjà ce que fait `agent_context`.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal, Optional

from fastmcp.server.dependencies import get_context
from fastmcp.server.transforms.visibility import disable_components, enable_components
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import access, deprecations, providers, db, tool_alias, tool_registry
from ..auth import hooks as auth_hooks
from ..tool_visibility import (
    PROTECTED_TOOLS, is_default_hidden, is_testable, namespace_of)
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_PAR_NOM = "/api/me/tools/{name}"
# Recherche : borne par défaut. Au-delà, l'agent relit le catalogue entier — c'est le
# signe que la requête était trop large, pas qu'il manque des résultats.
_SEARCH_LIMIT = 40


# --- Entrées ----------------------------------------------------------------

class ToolsListInput(BaseModel):
    """Aucun paramètre : la toolbox lue est celle du porteur du jeton."""


class ToolsRegistryInput(BaseModel):
    """Aucun paramètre — le registre est celui du serveur, pas d'une session."""


class ToolNameInput(BaseModel):
    name: str          # placeholder {name}, auto-mappé


class ToolToggleInput(BaseModel):
    # Placeholder {name} côté REST (auto-mappé) ; paramètre plat côté MCP.
    name: str = Field(description=(
        "Exact tool name (e.g. `attio_create_deal`, `linkedin_unipile_search`)."))


class ToolsSearchInput(BaseModel):
    op: Optional[Literal["list", "search"]] = Field(None, description=(
        "`list` | `search`; derived from `query` when omitted."))
    query: Optional[str] = Field(None, description=(
        'words to search (op=search), e.g. "linkedin message", "invoice".'))
    state: Optional[str] = Field(None, description="keep only the tools in this state.")
    limit: Optional[int] = Field(None, description=(
        "cap the entries (search: 40 by default; list: none)."))
    full: bool = Field(False, description=(
        "more description — list: one line per tool; search: whole docstrings."))


class ToolCallInput(BaseModel):
    name: str
    # Le corps ENTIER (cf. `body_field`). Il est LIBRE par nature : ce sont les
    # arguments de l'outil visé, dont aucun modèle statique ne connaît les champs.
    # Deux formes acceptées depuis toujours, et les deux le restent : l'objet
    # d'arguments nu, ou son enveloppe `{"arguments": {…}}`.
    arguments: Optional[dict] = None


# --- Sorties ----------------------------------------------------------------

class ToolState(BaseModel):
    """⚠️ `enabled` est un état de VISIBILITÉ, pas une autorisation (ADR 0031) :
    un outil visible peut très bien refuser à l'appel (credential absent, connecteur
    restreint dans l'org, connecteur non activé). `protected` = anti-lockout : ces
    outils-là ne peuvent pas être masqués, la bascule échouerait en 400."""
    name: str
    enabled: bool
    protected: bool


class ToolsListView(BaseModel):
    """Tous les outils du serveur, triés par nom, avec l'état PERSONNEL de l'appelant
    dans son org active. Inclut les désactivés (le middleware les retire de la liste
    MCP ; ils sont réinjectés ici, sinon la grille du dashboard ne pourrait plus les
    réactiver)."""
    tools: list[ToolState]


class RegistryEntry(BaseModel):
    """Entrée du registre résolu (ADR 0014). `description` est un RÉSUMÉ d'une ligne
    (la 1ʳᵉ ligne de la docstring, écrêtée), pas la fiche — pour ça, `…/detail`."""
    name: str
    description: str
    # ⚠️ TOUJOURS `"native"` depuis le retrait de la fédération MCP (2026-09-09,
    # ADR 0069) : plus aucun outil servi ne vient d'un serveur tiers. Gardé au
    # contrat (le dashboard le lit), pas parce qu'il discrimine encore.
    source: str
    # Jamais posé depuis le même retrait ; gardé pour la même raison.
    mcp: Optional[str] = None


class ToolsRegistryView(BaseModel):
    """Le registre BOOT, immunisé à la visibilité de session (#75) : il répond « cet
    outil existe-t-il dans le produit ? », jamais « m'est-il visible ici ? ». C'est ce
    qui alimente la résolution des marqueurs `<tool:slug>` d'un guide."""
    tools: list[RegistryEntry]
    count: int


class ToolConnector(BaseModel):
    name: str
    label: Optional[str] = None


class ToolDetailView(BaseModel):
    """La fiche d'un outil. `input_schema`/`output_schema` sont les JSON Schema dérivés
    par FastMCP — `output_schema` est souvent `null`, un outil n'est pas tenu d'en
    déclarer un. `testable` gate le bouton « tester » : seuls les connecteurs open-data
    en lecture seule le sont, jamais un outil à effet de bord."""
    name: str
    description: str
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    namespace: Optional[str] = None
    connector: Optional[ToolConnector] = None
    source: str                       # toujours 'native' (cf. RegistryEntry)
    enabled: bool
    protected: bool
    default_hidden: bool
    testable: bool


class ToolToggled(BaseModel):
    """L'état APRÈS la bascule, pas l'ordre reçu. `name` est le nom tel que l'appelant
    le VOIT : canonique sur la face REST, préfixé du tenant sur la face MCP."""
    ok: bool
    name: str
    enabled: bool


class ToolCallResult(BaseModel):
    """⚠️ **`ok: false` est une réponse 200, pas une erreur HTTP.** L'échec d'un outil
    est le RÉSULTAT du test — voir ce qu'il renvoie, y compris son message d'erreur,
    est précisément le but du bouton. Les 4xx sont réservés au fait de ne pas avoir pu
    lancer le test (outil inconnu, non testable, arguments invalides)."""
    ok: bool
    name: str
    # Résultat de l'outil, sérialisé défensivement (un outil peut rendre un objet
    # non-JSON, il devient alors sa représentation texte). Absent si `ok: false`.
    result: Optional[Any] = None
    elapsed_ms: Optional[int] = None
    # Message d'erreur de l'outil (ou « timeout (>45s) »). Absent si `ok: true`.
    error: Optional[str] = None


# --- Handlers ---------------------------------------------------------------

def _org_de_visibilite(sub: str) -> int:
    """L'org qui scope les préférences de visibilité de `sub` (toggles stockés par
    `(sub, org)`, ADR 0015/0023). Lue par le seam unique `access.current_org` — jeton
    d'appel `_org`, sinon consultation, sinon maison — jamais `org_store.get_active_org`
    en direct, qui ignorerait le jeton. `0` = aucune org. SQL synchrone : au thread."""
    return access.current_org(sub) or 0


def _disabled_tools(sub: str) -> list:
    """Outils désactivés par `sub` dans son org active — SQL synchrone (`current_org` +
    `list_user_disabled_tools`) : à appeler via `run_in_threadpool` depuis un handler async."""
    return db.list_user_disabled_tools(sub, _org_de_visibilite(sub))


async def _tool_by_name(name: str):
    """Objet Tool FastMCP par nom (ou None). `run_middleware=False` : hors session MCP
    (contexte REST) la chaîne de middleware n'a pas de Context et lèverait."""
    instance = tool_registry.bound_instance()
    if instance is None:
        return None
    for t in await instance.list_tools(run_middleware=False):
        if t.name == name:
            return t
    return None


async def _list(ctx: ResolvedCtx, inp: ToolsListInput) -> dict:
    instance = tool_registry.bound_instance()
    all_names: set[str] = set()
    if instance is not None:
        # run_middleware=False : appelé hors session MCP (contexte REST), la
        # chaîne de middleware n'a pas de Context FastMCP et lèverait → on
        # veut la liste statique complète, le filtrage disabled est fait
        # juste après via `disabled`.
        all_names = {t.name for t in await instance.list_tools(run_middleware=False)}

    disabled = set(await run_in_threadpool(_disabled_tools, ctx.sub))
    # Le middleware retire déjà les disabled de `list_tools` selon le sub
    # courant (celui de la requête REST = même token). On ré-ajoute donc
    # les disabled pour avoir la vue complète.
    all_names |= disabled

    return {
        "tools": [
            {"name": n, "enabled": n not in disabled,
             "protected": n in PROTECTED_TOOLS}
            for n in sorted(all_names)
        ],
    }


async def _registry(ctx: ResolvedCtx, inp: ToolsRegistryInput) -> dict:
    try:
        reg = await tool_registry.build_registry(tool_registry.bound_instance())
    except Exception as e:  # noqa: BLE001 — l'échec de listing EST le message rendu
        raise AuthzDenied(500, f"list_tools_failed:{e}")
    out = sorted(reg.values(), key=lambda e: e["name"])
    return {"tools": out, "count": len(out)}


def _nom_canonique(sub: str, name: str) -> tuple[str, str]:
    """`(nom canonique, préfixe du tenant)`. Le nom peut arriver sous la forme du tenant
    (`acme_doc`, ce qu'un agent lit dans sa liste) ou sous un nom DÉPRÉCIÉ (#519, que
    `tools/list` sert encore) : la denylist s'écrit en canonique — sinon le même outil
    s'y retrouverait deux fois, et la bascule ne mordrait plus après un changement de
    préfixe. `prefix_for` lit un registre en mémoire : aucun accès base."""
    prefix = tool_alias.prefix_for(sub)
    return deprecations.tool_canonique(tool_alias.canonical(name, prefix)), prefix


def _nom_montre(ctx: ResolvedCtx, name: str, prefix: str) -> str:
    """Le nom tel que l'appelant le VOIT : l'agent lit les noms préfixés du tenant
    (`ToolAliasMiddleware`), le dashboard les noms canoniques de `GET /api/me/tools`."""
    return tool_alias.public(name, prefix) if ctx.channel == "mcp" else name


async def _disable(ctx: ResolvedCtx, inp: ToolToggleInput) -> dict:
    """Masque un outil pour l'appelant, dans son org active."""
    name, prefix = _nom_canonique(ctx.sub, inp.name)
    instance = tool_registry.bound_instance()
    # Hors session (REST), `list_tools` rend tout le catalogue ; dans une session MCP,
    # la liste qu'elle VOIT — un outil qu'on ne voit pas n'a pas à être masqué.
    connus = ({t.name for t in await instance.list_tools(run_middleware=False)}
              if instance is not None else set())
    if name not in connus:
        raise AuthzDenied(404, f"unknown_tool:{name}",
                          f"Unknown tool `{name}`. Use oto_list_my_tools to see "
                          "available names.")
    if name in PROTECTED_TOOLS:
        raise AuthzDenied(400, f"protected_tool:{name}",
                          f"`{name}` is protected (toolset management, context switching "
                          "or usage loop) — refusing to disable.")

    def _ecrire():
        org = _org_de_visibilite(ctx.sub)
        db.add_user_disabled_tool(ctx.sub, name, org)
        db.remove_user_enabled_tool(ctx.sub, name, org)  # lève un éventuel override positif

    await run_in_threadpool(_ecrire)
    if ctx.channel == "mcp":
        # La session courante le perd TOUT DE SUITE (`tools/list_changed`), sans
        # attendre la prochaine.
        await disable_components(get_context(), names={name}, components={"tool"})
    return {"ok": True, "name": _nom_montre(ctx, name, prefix), "enabled": False}


async def _enable(ctx: ResolvedCtx, inp: ToolToggleInput) -> dict:
    """Démasque un outil pour l'appelant, dans son org active.

    Visibilité seule (ADR 0031) : activer est une préférence d'AFFICHAGE, jamais une
    autorisation. Rendre un outil visible ne donne pas accès à son credential : l'accès
    réel est gardé à l'appel (`resolve_credential`, le cran d'activation, la résolution
    du credential bridge, ADR 0034)."""
    name, prefix = _nom_canonique(ctx.sub, inp.name)

    def _ecrire():
        org = _org_de_visibilite(ctx.sub)
        db.remove_user_disabled_tool(ctx.sub, name, org)
        # Override positif requis pour rendre visible un masqué-par-défaut.
        if is_default_hidden(name):
            db.add_user_enabled_tool(ctx.sub, name, org)

    await run_in_threadpool(_ecrire)
    if ctx.channel == "mcp":
        await enable_components(get_context(), names={name}, components={"tool"})
    return {"ok": True, "name": _nom_montre(ctx, name, prefix), "enabled": True}


async def _search(ctx: ResolvedCtx, inp: ToolsSearchInput) -> dict:
    """Le catalogue ENTIER avec l'état de chaque outil pour l'appelant, en liste groupée
    ou en recherche lexicale. Le calcul de l'état vit dans `tools/catalogue.py`, noyau
    de cette face ; la grille du dashboard (`_list`) pose une autre question."""
    from ..tools import catalogue
    from .connectors.selection import _toolbox_scope
    op = inp.op or ("search" if inp.query else "list")
    if op == "search" and not (inp.query or "").strip():
        raise AuthzDenied(400, "query_required",
                          "op=search : `query` requis (les mots à chercher).")
    if op == "list" and inp.query:
        raise AuthzDenied(400, "query_on_list",
                          "op=list ne filtre pas par `query` — pour chercher, op=search.")
    if inp.state is not None and inp.state not in catalogue.ETATS:
        raise AuthzDenied(400, "invalid_state",
                          f"`state` ∈ {' | '.join(catalogue.ETATS)}.")
    prefix = tool_alias.prefix_for(ctx.sub)
    entries = await catalogue.catalogue_avec_etat(get_context(), ctx.sub, prefix)
    par_etat = {e: sum(1 for x in entries if x["state"] == e) for e in catalogue.ETATS}
    out: dict = {"op": op, "catalog_total": len(entries), "catalog_by_state": par_etat}
    # L'aveu du décalage de boîte (#577, signaux #616/#639) : la session a été montée
    # pour l'org MAISON au handshake, l'appel épingle peut-être une autre org — les
    # outils de ses connecteurs ne sont alors PAS listés, tout en restant appelables.
    # Ici est l'endroit où un agent cherche un outil, et où il concluait « indisponible ».
    tb = await run_in_threadpool(_toolbox_scope, ctx.sub)
    if tb:
        out["toolbox_scope"] = tb
    if inp.state:
        entries = [e for e in entries if e["state"] == inp.state]
        out["state"] = inp.state
    if op == "search":
        entries = tool_registry.match(inp.query, entries)
        out["query"] = inp.query
        if not entries:
            # Zéro résultat lexical ≠ « oto ne sait pas faire » : on rend la carte des
            # capacités, l'agent repart du domaine au lieu de conclure à une lacune.
            out["namespaces"] = providers.render_namespace_catalog()
            out["hint"] = catalogue.hint_zero_resultat(tb)
    # oto#42 : `total` décrit le jeu qu'il accompagne — sur une recherche, le nombre de
    # CORRESPONDANCES, jamais le catalogue entier (rendu à part, `catalog_total`).
    out["total"] = len(entries)
    cap = (inp.limit if inp.limit is not None
           else (_SEARCH_LIMIT if op == "search" else None))
    shown = entries[:cap] if cap else entries
    out["shown"] = len(shown)
    if len(shown) < len(entries):
        out["truncated"] = True
        out["hint_truncated"] = (
            f"{len(entries)} outils correspondent, {len(shown)} rendus. Affine la "
            "recherche, ou relance avec `limit` plus haut pour les voir tous.")
    out["legend"] = catalogue.LEGENDE
    if op == "search":
        cle = "description_full" if inp.full else "description"
        out["tools"] = [{"name": e["name"], "namespace": e["namespace"],
                         "state": e["state"], "description": e[cle]} for e in shown]
    elif inp.full:
        out["tools"] = [{k: e[k] for k in ("name", "namespace", "state", "description")}
                        for e in shown]
    else:
        out["connectors"] = catalogue.grouper_par_connecteur(shown)
        out["projection"] = ("un groupe par connecteur, avec ses outils par nom ; "
                             "`full=True` rend une ligne de description par outil, "
                             "`oto_tool_schema(name)` le détail d'un outil.")
    return out


async def _detail(ctx: ResolvedCtx, inp: ToolNameInput) -> dict:
    tool = await _tool_by_name(inp.name)
    if tool is None:
        raise AuthzDenied(404, f"unknown_tool:{inp.name}")
    ns = namespace_of(inp.name)
    conn = providers.connector_for_namespace(ns)
    disabled = set(await run_in_threadpool(_disabled_tools, ctx.sub))
    return {
        "name": inp.name,
        "description": (tool.description or "").strip(),
        "input_schema": getattr(tool, "parameters", None),
        "output_schema": getattr(tool, "output_schema", None),
        "namespace": ns,
        "connector": ({"name": conn.name, "label": conn.label} if conn else None),
        "source": "native",
        "enabled": inp.name not in disabled,
        "protected": inp.name in PROTECTED_TOOLS,
        "default_hidden": is_default_hidden(inp.name),
        "testable": is_testable(inp.name),
    }


async def _call(ctx: ResolvedCtx, inp: ToolCallInput) -> dict:
    """Exécute un outil TESTABLE sous l'identité de l'appelant (bouton « tester »
    du dashboard). Bornée aux connecteurs open-data en lecture seule
    (`is_testable`) — jamais un outil à effet de bord. Les gates de call-time
    (credential, RBAC connecteur, activation) s'appliquent normalement : le
    sub-override REST fait résoudre la bonne identité (`resolve_api_key`/
    `current_org`). L'erreur d'un outil est renvoyée EN DONNÉE (`ok:false`) —
    voir ce que renvoie l'outil (y compris son erreur) EST le but du test."""
    if not is_testable(inp.name):
        raise AuthzDenied(403, f"not_testable:{inp.name}")
    tool = await _tool_by_name(inp.name)
    if tool is None:
        raise AuthzDenied(404, f"unknown_tool:{inp.name}")
    fn = getattr(tool, "fn", None)
    if fn is None:
        raise AuthzDenied(400, f"not_callable:{inp.name}")
    # Accepte {"arguments": {...}} ou l'objet d'arguments brut. Corps absent, illisible
    # ou non-objet (une liste) ⇒ `arguments` reste None ⇒ aucun argument, comme avant.
    body = inp.arguments if inp.arguments is not None else {}
    args = body.get("arguments") if isinstance(body, dict) and "arguments" in body else body
    if not isinstance(args, dict):
        args = {}

    async def _invoke():
        if asyncio.iscoroutinefunction(fn):
            return await fn(**args)
        return await run_in_threadpool(lambda: fn(**args))

    started = time.monotonic()
    with auth_hooks.sub_override(ctx.sub):
        try:
            result = await asyncio.wait_for(_invoke(), timeout=45)
        except asyncio.TimeoutError:
            return {"ok": False, "name": inp.name, "error": "timeout (>45s)"}
        except TypeError as e:
            # Mauvais arguments (param inconnu / manquant) : signal actionnable.
            raise AuthzDenied(400, f"bad_arguments:{e}")
        # noqa: SILENT — voir l'erreur de l'outil EST le but du bouton « tester »
        except Exception as e:  # noqa: BLE001 — l'erreur d'outil est le résultat
            return {"ok": False, "name": inp.name, "error": str(e)}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Sérialisation défensive : un tool peut renvoyer un objet non-JSON.
    try:
        safe = json.loads(json.dumps(result, default=str, ensure_ascii=False))
    # noqa: SILENT — sérialisation défensive : un tool peut rendre un objet non-JSON
    except Exception:  # noqa: BLE001
        safe = str(result)
    return {"ok": True, "name": inp.name, "result": safe, "elapsed_ms": elapsed_ms}


_DOC_LIST = (
    "Tous les outils du serveur avec MON état de visibilité dans l'org active. "
    "`enabled` est une préférence d'affichage, PAS une autorisation : un outil visible "
    "peut refuser à l'appel (credential absent, connecteur restreint ou non activé). "
    "`protected` marque les outils anti-lockout, qu'on ne peut pas masquer."
)
_DOC_REGISTRY = (
    "Le registre résolu des outils du produit : nom, résumé d'une ligne, origine "
    "(native ou fédérée). C'est la matière des marqueurs `<tool:slug>` d'un guide "
    "et de l'autocomplétion. Immunisé à la visibilité de session — il dit ce qui "
    "EXISTE, pas ce qui m'est visible."
)
# Les trois textes servis aux agents depuis l'origine (`tools/meta.py`, avant #429),
# repris tels quels : la description d'une capacité vaut pour ses deux faces.
_DOC_SEARCH = (
    "The oto tool CATALOG — EVERY tool of the platform (~725), each with its STATE\n"
    "for you: `installed` (in your toolbox: call it directly), `installable`\n"
    "(callable right now with `oto_call`, installed durably with\n"
    "`oto_connector(op='select', name=<connector>)`) or `not_exposed` (NOT\n"
    "callable: the connector is not opened to your organization, or the tool is\n"
    "beyond your role — an org admin opens it). A tool absent from your toolbox is\n"
    "never a missing capability: it is here, with the state that says what to do.\n"
    "\n"
    "op=list (default without `query`) → the whole catalog GROUPED by connector:\n"
    "`{namespace, connector, label, state, tools: [names]}` (~25k chars in all).\n"
    "`full=True` flattens it, one entry per tool with a one-line description\n"
    "(~115k chars: prefer `state=` or a search). `state=installed|installable|\n"
    "not_exposed` keeps one state.\n"
    "op=search (default with `query`) → tools RANKED by how many words of `query`\n"
    "match their name, their connector's catalog line and their description.\n"
    "LEXICAL, docstrings in ENGLISH: zero result means « rephrase, try English, or\n"
    "op=list » — never « oto cannot do this ». 40 entries by default (`limit`),\n"
    "one-line descriptions; `full=True` = whole descriptions.\n"
    "\n"
    "Entry point of the deferred mode — `oto_list_my_tools` → `oto_tool_schema(name)`\n"
    "(the exact arguments, read BEFORE calling) → `oto_call` — the way an agent\n"
    "reaches oto without loading ~725 schemas."
)
_DOC_DISABLE = (
    "Disable a tool for the current user — persistent across sessions.\n"
    "\n"
    "The tool disappears from the visible list immediately (the server\n"
    "notifies the client via tools/list_changed). Re-enable with\n"
    "`oto_enable_tool`."
)
_DOC_ENABLE = (
    "Re-enable a previously disabled tool for the current user. On a tool hidden\n"
    "by default at platform level, also sets the positive override that lifts it."
)
_DOC_DETAIL = (
    "La fiche complète d'un outil : description, schémas d'entrée et de sortie dérivés "
    "par FastMCP, connecteur d'origine, état personnel et testabilité. Alimente le "
    "panneau « en savoir plus » et, si l'outil est testable, le formulaire de test."
)
_DOC_CALL = (
    "Exécute un outil TESTABLE sous mon identité (bouton « tester »). Borné aux "
    "connecteurs open-data en lecture seule — jamais un outil à effet de bord. Le corps "
    "est l'objet d'arguments, nu ou enveloppé dans `{\"arguments\": {…}}`. ⚠️ L'erreur "
    "de l'outil revient EN DONNÉE (`ok: false` en 200) : la voir est le but du test. "
    "Les 4xx disent qu'on n'a pas pu lancer, pas que l'outil a échoué."
)

CAPABILITIES += [
    Capability(
        key="me.tools.list", handler=_list, Input=ToolsListInput, authz=SUB_ONLY,
        Output=ToolsListView, description=_DOC_LIST,
        mcp=None,   # la face agent est `me.tools.search` : autre question (#429)
        rest=RestBinding("GET", "/api/me/tools"),
    ),
    # La face AGENT de la toolbox : le catalogue avec l'état de chaque outil, en liste
    # ou en recherche. MCP seul — le dashboard peint sa grille avec `me.tools.list`.
    Capability(
        key="me.tools.search", handler=_search, Input=ToolsSearchInput, authz=SUB_ONLY,
        description=_DOC_SEARCH,
        mcp="oto_list_my_tools",
    ),
    # ⚠️ `registry` AVANT `{name}` : Starlette prend le premier match, et `{name}`
    # capturerait « registry » comme un nom d'outil. Cet ordre EST le contrat.
    Capability(
        key="me.tools.registry", handler=_registry, Input=ToolsRegistryInput,
        authz=SUB_ONLY, Output=ToolsRegistryView, description=_DOC_REGISTRY,
        mcp=None,
        rest=RestBinding("GET", "/api/me/tools/registry"),
    ),
    Capability(
        key="me.tools.disable", handler=_disable, Input=ToolToggleInput, authz=SUB_ONLY,
        Output=ToolToggled, description=_DOC_DISABLE,
        mcp="oto_disable_tool",
        # ⚠️ POST MASQUE : le chemin nomme la ligne de denylist (cf. en-tête du module).
        rest=RestBinding("POST", _PAR_NOM),
    ),
    Capability(
        key="me.tools.enable", handler=_enable, Input=ToolToggleInput, authz=SUB_ONLY,
        Output=ToolToggled, description=_DOC_ENABLE,
        mcp="oto_enable_tool",
        rest=RestBinding("DELETE", _PAR_NOM),
    ),
    Capability(
        key="me.tools.detail", handler=_detail, Input=ToolNameInput, authz=SUB_ONLY,
        Output=ToolDetailView, description=_DOC_DETAIL,
        mcp=None,
        rest=RestBinding("GET", _PAR_NOM + "/detail"),
    ),
    Capability(
        key="me.tools.call", handler=_call, Input=ToolCallInput, authz=SUB_ONLY,
        Output=ToolCallResult, description=_DOC_CALL,
        mcp=None,   # `oto_call` est le dispatch MCP, MCP-only par nature
        rest=RestBinding("POST", _PAR_NOM + "/call", body_field="arguments"),
    ),
]

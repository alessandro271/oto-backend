"""Catalogue des capacités (ADR 0030, object-browser admin).

Expose le registre `CAPABILITIES` comme données : clé, nom MCP, bindings REST,
description et **JSON Schema** de l'Input (dérivé de pydantic v2 `model_json_schema()`).
L'UI admin (navigateur d'objets, niveau PLATEFORME) se peint à partir de ce
catalogue — *derive don't duplicate* : pas d'écran codé par capacité.

REST-only (`mcp=None`), gate `PLATFORM_ADMIN`. Le catalogue se liste lui-même (sans
conséquence). N'expose aucune valeur de secret — uniquement la forme des opérations.

Même matière, même garde : le document OpenAPI d'ADMINISTRATION (`admin.openapi`,
`GET /api/admin/openapi.json`, oto-backend#1196) — toutes les opérations `/api/admin/*`,
que le document public (`/api/openapi.json`, sans authentification) ne publie pas.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ._authz import PLATFORM_ADMIN
from ._types import Capability, ResolvedCtx, RestBinding
from . import registry
from .registry import CAPABILITIES


class _CatalogInput(BaseModel):
    pass


def _authz_label(rule) -> str:
    """Libellé best-effort de la règle d'autz (fonction nommée ou closure)."""
    name = getattr(rule, "__name__", None)
    return name if name and name != "rule" else "scoped"


def _binding_view(b: RestBinding) -> dict:
    return {"method": b.verb, "path": b.path}


def _catalog(ctx: ResolvedCtx, inp: _CatalogInput) -> dict:
    out = []
    for cap in registry.CAPABILITIES:
        try:
            schema = cap.Input.model_json_schema()
        # noqa: SILENT — schéma illisible ⇒ entrée de catalogue amputée, jamais absente
        except Exception:
            schema = {}
        out.append({
            "key": cap.key,
            "mcp": cap.mcp,
            "rest": [_binding_view(b) for b in cap.rest_bindings()],
            "description": cap.description,
            "authz": _authz_label(cap.authz),
            "input_schema": schema,
        })
    out.sort(key=lambda c: c["key"])
    return {"capabilities": out}


class _OpenApiAdminInput(BaseModel):
    pass


class DocumentOpenApi(BaseModel):
    """Un document OpenAPI 3.1 — DÉCRIT la réponse, ne la valide pas (régime de
    `Capability.Output`). Son contenu est celui de `openapi.build(admin=True)`."""
    model_config = ConfigDict(extra="allow")

    openapi: str
    info: dict
    paths: dict
    components: dict
    security: list


def _openapi_admin(ctx: ResolvedCtx, inp: _OpenApiAdminInput) -> dict:
    """Sans la table de routes vivante (un handler ne voit pas la requête, ADR 0004) :
    toute route `/api/admin/*` est une capacité (`test_rest_modules_are_capabilities`),
    donc le registre suffit — et un banc prouve que ce document égale celui que la CI
    dérive de `server.routes_rest` (`tests/test_openapi_admin_1196.py`). Import
    paresseux : `openapi` lit le registre que ce paquet remplit."""
    from .. import openapi
    return openapi.build(admin=True)


CAPABILITIES += [
    Capability(
        key="admin.capabilities",
        handler=_catalog,
        Input=_CatalogInput,
        authz=PLATFORM_ADMIN,
        description="List the platform capability registry (keys, surfaces, input JSON schemas).",
        rest=RestBinding("GET", "/api/admin/capabilities"),
    ),
    Capability(
        key="admin.openapi",
        handler=_openapi_admin,
        Input=_OpenApiAdminInput,
        Output=DocumentOpenApi,
        authz=PLATFORM_ADMIN,
        description=(
            "The OpenAPI document of the platform administration surface: every "
            "/api/admin/* operation, derived from the capability registry. The public "
            "document (/api/openapi.json) does not contain them."),
        rest=RestBinding("GET", "/api/admin/openapi.json"),
    ),
]

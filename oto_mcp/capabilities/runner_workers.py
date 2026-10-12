"""Déclarer, lister, révoquer les workers de plateforme — REST seule
(`POST /api/admin/runner/workers`), super admin.

Le seul endroit d'où un worker naît. Il en sort avec son secret (`otow_…`),
montré UNE fois : le backend n'en garde que le haché. Le secret va dans
l'environnement de l'unité systemd du worker (`OTO_WORKER_SECRET`) — et c'est
tout ce que le worker possède, avec l'adresse du backend.

⚠️ **Aucune face MCP, super admin seul** (10/10/2026). `op=create` REND un secret
brut : servi en outil MCP (`oto_admin_runner_worker`, alors PLATFORM_ADMIN), il
transitait dans le contexte d'un modèle — et de tout agent hébergé d'un admin, sous
son jeton de délégation. Même règle que la pose de secret (« secret brut jamais en
argument ni en réponse MCP », `docs/couches-et-capacites.md`). Et un secret de worker
ouvre la file de TOUTES les orgs, avec un jeton délégué au nom de chaque porteur :
le geste est celui du tout-puissant, pas de l'opérateur de supervision.

⚠️ DETTE : le secret n'a ni échéance ni rotation — `revoke` puis `create` est le seul
geste. Une échéance demande une colonne (migration), pas faite ici.
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, RootModel

from .. import db
from ._authz import SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class WorkerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create", "list", "revoke"]
    label: Optional[str] = None
    worker_sub: Optional[str] = None


class WorkerRow(BaseModel):
    worker_sub: str
    label: Optional[str] = None
    created_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    revoked_at: Optional[str] = None
    declared: Optional[bool] = None


class WorkerCreated(BaseModel):
    worker: WorkerRow
    secret: str
    note: str


class WorkerList(BaseModel):
    workers: list[WorkerRow]


class WorkerRevoked(BaseModel):
    revoked: str


class WorkerOut(RootModel[Union[WorkerCreated, WorkerList, WorkerRevoked]]):
    """Trois `op`, trois formes sans clé commune : l'UNION des verbes, pas une
    enveloppe vide (ADR 0059, cf. `test_capability_outputs`)."""


def _workers(ctx: ResolvedCtx, inp: WorkerInput) -> dict:
    if inp.op == "create":
        if not (inp.label or "").strip():
            raise AuthzDenied(400, "missing_fields",
                              "create exige `label` — le nom lisible du worker "
                              "(ex. « oto-platform worker 1 »).")
        w = db.create_platform_worker(inp.label)
        return {"worker": {k: (str(v) if k == "created_at" else v)
                           for k, v in w.items() if k != "secret"},
                "secret": w["secret"],
                "note": "Le secret est montré UNE fois. Pose-le dans "
                        "OTO_WORKER_SECRET de l'unité du worker ; le backend "
                        "n'en garde que le haché."}
    if inp.op == "list":
        return {"workers": [{k: (str(v) if hasattr(v, "isoformat") else v)
                             for k, v in w.items()}
                            for w in db.list_platform_workers()]}
    if not inp.worker_sub:
        raise AuthzDenied(400, "missing_fields", "revoke exige `worker_sub`.")
    if not db.revoke_platform_worker(inp.worker_sub):
        raise AuthzDenied(404, "worker_not_found",
                          f"aucun worker actif `{inp.worker_sub}` — inconnu, "
                          "ou déjà révoqué.")
    return {"revoked": inp.worker_sub}


CAPABILITIES += [
    Capability(
        key="platform.runner.worker", handler=_workers, Input=WorkerInput,
        Output=WorkerOut, authz=SUPER_ADMIN,
        mcp=None,   # secret brut en réponse : jamais dans un contexte de modèle
        rest=RestBinding("POST", "/api/admin/runner/workers"),
        description=(
            "[super admin, REST only] Platform workers of the hosted runner — machine "
            "secrets, not accounts. op=create (label → secret shown ONCE, put it in "
            "the worker's OTO_WORKER_SECRET) / list / revoke (worker_sub). A worker "
            "has no org: the backend commands it a complete job (org, delegated "
            "token, model key, procedure) at each poll."
        ),
    ),
]

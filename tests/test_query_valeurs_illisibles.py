"""Une valeur de query string vide, illisible ou hors calendrier rend un 4xx NOMMÉ,
jamais un `500 internal_error` (durcissement CASA 6.2.1, trouvé par un scan DAST
authentifié).

Ce qui tombait en 500, route servie par le vrai adaptateur REST contre un vrai
PostgreSQL :

1. **Bornes horaires** (`since`, `until`, `before_at`) des relevés d'usage
   (`usage/tools`, `usage/calls`, leurs jumelles de service) et de l'export d'audit :
   la chaîne partait au SQL en `%s::timestamptz`. Désormais une valeur VIDE est un
   paramètre ABSENT (`_types.vide_en_absent`) et une valeur que PostgreSQL ne lit pas
   comme un instant fini — `garbage`, `2026-13-45`, `infinity` — est refusée
   `400 invalid_timestamp` en nommant le paramètre (`db.BorneIllisible`). C'est
   PostgreSQL qui juge, pas un second analyseur Python : il est celui qui lira.
2. **`days` énorme** sur `monitoring/calls`, `monitoring/runs`, `monitoring/gaps`,
   `monitoring/tool-quality` : `make_interval(days => bigint)` n'existe pas, une
   fenêtre de mille ans sort du calendrier, `timedelta` déborde. Écrêté à [1, 365],
   comme les autres agrégats du store.
3. **Octet NUL** (`?tool=%00`) sur n'importe quelle route générée : aucune colonne texte
   ne le porte, le pilote levait en pleine lecture. Refusé `400 invalid_query` par
   l'adaptateur, avant tout handler.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp.capabilities import _authz, _rest_adapter, audit_log, monitoring
from oto_mcp.capabilities import org_monitoring as om
from oto_mcp.capabilities._types import Capability, RestBinding, vide_en_absent
from oto_mcp.capabilities.registry import CAPABILITIES

_SUB = "sub-valeurs-illisibles"
_FORME = "is not a readable timestamp"


def _cap(cle):
    return next(c for c in CAPABILITIES if c.key == cle)


def _json_error(_req, status, code, message=None, **_kw):
    return JSONResponse({"error": code, "detail": message}, status_code=status)


def _json_response(_req, payload, status=200):
    return JSONResponse(payload, status_code=status)


async def _auth(_req, _verifier, **_kw):
    return _SUB, None


async def _servir(cap, chemin: str, query: str, path_params: dict):
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    handler = _rest_adapter._make_handler(cap, cap.rest, None, _auth,
                                          _json_response, _json_error)
    rep = await handler(Request({"type": "http", "method": "GET", "path": chemin,
                                 "headers": [], "query_string": query.encode(),
                                 "path_params": path_params}, _receive))
    return rep.status_code, json.loads(bytes(rep.body))


# ── Sans base : la normalisation et la garde de l'adaptateur ───────────────────


@pytest.mark.parametrize("brut, attendu", [
    ("", None), ("   ", None), (None, None),
    ("2026-11-01", "2026-11-01"), ("garbage", "garbage"), (7, 7)])
def test_une_valeur_vide_est_un_parametre_absent(brut, attendu):
    assert vide_en_absent(brut) == attendu


@pytest.mark.parametrize("Input, champs", [
    (om.OrgBillableToolsInput, ("since", "until")),
    (om.OrgBillableCallsInput, ("since", "until", "before_at")),
    (om.OrgMonitoringInput, ("since", "until")),
    (audit_log.AuditExportInput, ("since", "until")),
    (om.OrgConnectionsInput, ("since",)),
])
def test_les_bornes_vides_arrivent_absentes_au_handler(Input, champs):
    extra = {"op": "export"} if Input is om.OrgMonitoringInput else {}
    inp = Input(org_id=7, **extra, **{c: "" for c in champs})
    assert all(getattr(inp, c) is None for c in champs)
    # Une borne NON vide n'est pas réécrite : c'est elle que la réponse réécho.
    inp = Input(org_id=7, **extra, **{c: "2026-11-01T08:00:00Z" for c in champs})
    assert all(getattr(inp, c) == "2026-11-01T08:00:00Z" for c in champs)


@pytest.mark.parametrize("brut, attendu", [
    (None, None), (-99999999999, 1), (0, 1), (30, 30), (99999999999, 365)])
def test_days_des_appels_est_ecrete_avant_le_plancher(brut, attendu):
    """`_horizon` fait `timedelta(days=…)` : il faut la valeur écrêtée AVANT lui."""
    # La lentille d'org construit ce modèle (`org_monitoring._calls`) : un seul écrêtage.
    assert monitoring.CallsInput(days=brut).days == attendu


class _EchoInput(BaseModel):
    tool: str | None = None


@pytest.mark.asyncio
@pytest.mark.parametrize("query, nomme", [
    ("tool=%00", "'tool'"), ("tool=a%00b", "'tool'"), ("t%00ol=x", "'t\\x00ol'")])
async def test_un_octet_nul_est_refuse_avant_tout_handler(query, nomme):
    vu = []
    cap = Capability(key="test.echo", handler=lambda ctx, inp: vu.append(inp) or {},
                     Input=_EchoInput, authz=lambda raw, inp: None, mcp=None,
                     rest=RestBinding("GET", "/api/test/echo"))
    code, corps = await _servir(cap, "/api/test/echo", query, {})
    assert (code, corps["error"]) == (400, "invalid_query")
    assert nomme in corps["detail"]
    assert not vu, "le handler n'est pas appelé"


# ── Les routes servies, contre un vrai PostgreSQL ──────────────────────────────

_BORNE = (400, "invalid_timestamp")
_NUL = (400, "invalid_query")

CAS = [
    # La requête du scan, et ses voisines : vide = absent.
    ("org.usage.tools", "usage/tools", "tool=null&since=&until=", 200),
    ("org.usage.tools", "usage/tools", "since=&until=", 200),
    ("org.usage.tools", "usage/tools", "since=%20%20", 200),
    ("org.usage.tools", "usage/tools", "since=garbage", (*_BORNE, "since")),
    ("org.usage.tools", "usage/tools", "until=2026-13-45", (*_BORNE, "until")),
    ("org.usage.tools", "usage/tools", "until=2026-01-01T10:00:00%2B16:00", (*_BORNE, "until")),
    ("org.usage.tools", "usage/tools", "since=infinity", (*_BORNE, "since")),
    ("org.usage.tools", "usage/tools", "until=-infinity", (*_BORNE, "until")),
    ("org.usage.tools", "usage/tools", "tool=%00", _NUL),
    ("org.usage.tools", "usage/tools", "since=%00", _NUL),
    ("org.usage.calls", "usage/calls", "tool=x&since=&until=", 200),
    ("org.usage.calls", "usage/calls", "tool=x&until=infinity", (*_BORNE, "until")),
    ("org.usage.calls", "usage/calls", "tool=x&before_at=garbage&before_id=1",
     (*_BORNE, "before_at")),
    ("org.usage.calls", "usage/calls", "tool=x&before_at=&before_id=1", 200),
    ("org.usage.calls", "usage/calls", "tool=x&limit=abc", (400, "invalid_input", None)),
    ("org.usage.connections", "usage/connections", "since=", 200),
    ("org.usage.connections", "usage/connections", "since=garbage",
     (400, "invalid_input", None)),
    ("org.audit_log.export", "audit-log/export", "since=&until=", 200),
    ("org.audit_log.export", "audit-log/export", "since=garbage", (*_BORNE, "since")),
    ("org.audit_log.export", "audit-log/export", "until=infinity", (*_BORNE, "until")),
    ("org.monitoring.calls", "monitoring/calls", "days=99999999999", 200),
    ("org.monitoring.calls", "monitoring/calls", "days=2147483647", 200),
    ("org.monitoring.calls", "monitoring/calls", "days=1000000", 200),
    ("org.monitoring.calls", "monitoring/calls", "days=-99999999999", 200),
    ("org.monitoring.calls", "monitoring/calls", "tool=null", 200),
    ("org.monitoring.calls", "monitoring/calls", "days=abc", (400, "invalid_input", None)),
    ("org.monitoring.calls", "monitoring/calls", "limit=abc", (400, "invalid_input", None)),
    ("org.monitoring.calls", "monitoring/calls", "tool=%00", _NUL),
    ("org.monitoring.runs", "monitoring/runs", "days=99999999999", 200),
    ("org.monitoring.gaps", "monitoring/gaps", "days=2147483647", 200),
    ("org.monitoring.tool_quality", "monitoring/tool-quality", "days=2147483647", 200),
    ("org.monitoring.signals", "monitoring/signals",
     "op=signals&tool=null&since=&until=", 200),
    ("org.monitoring.signals", "monitoring/signals", "op=signals&limit=abc",
     (400, "invalid_input", None)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cle, suffixe, query, attendu", CAS,
                         ids=[f"{c[1]}?{c[2]}" for c in CAS])
async def test_la_route_servie_ne_rend_jamais_un_500(live, monkeypatch, cle, suffixe,
                                                    query, attendu):
    from oto_mcp import org_store

    org = org_store.create_org("Valeurs illisibles", created_by=_SUB)
    monkeypatch.setattr(_authz.roles, "is_org_member", lambda sub, org_id: True)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, org_id: True)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    code, corps = await _servir(_cap(cle), f"/api/orgs/{org}/{suffixe}", query,
                                {"id": str(org)})
    if attendu == 200:
        assert code == 200, corps
        return
    statut, erreur, nomme = attendu if len(attendu) == 3 else (*attendu, None)
    assert (code, corps["error"]) == (statut, erreur), corps
    if erreur == "invalid_timestamp":
        assert corps["detail"].startswith(f"`{nomme}` {_FORME}"), corps


def test_une_borne_vide_rend_la_fenetre_par_defaut(live, monkeypatch):
    """`?since=&until=` lit la même fenêtre que sans paramètre : la largeur maximale,
    bornée à maintenant — pas une fenêtre vide, pas une erreur."""
    import asyncio
    from datetime import datetime

    from oto_mcp import db, org_store

    org = org_store.create_org("Bornes vides", created_by=_SUB)
    monkeypatch.setattr(_authz.roles, "is_org_member", lambda sub, org_id: True)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    code, corps = asyncio.run(_servir(_cap("org.usage.tools"),
                                      f"/api/orgs/{org}/usage/tools",
                                      "tool=null&since=&until=", {"id": str(org)}))
    assert code == 200, corps
    debut, fin = (datetime.fromisoformat(corps[k].replace("Z", "+00:00"))
                  for k in ("since_effectif", "until_effectif"))
    assert (fin - debut).days == db.RELEVE_FENETRE_MAX_JOURS
    assert corps["tools"] == []

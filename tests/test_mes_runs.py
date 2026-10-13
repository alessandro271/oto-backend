"""`GET /api/me/runs` — la lentille MEMBRE des runs (la page d'onboarding la lit pour
cocher « premier process déroulé » chez un membre, à qui `org.monitoring.runs` est
fermé).

Ce que la capacité promet :
1. elle ne lit QUE les runs de l'appelant, sous l'org active — jamais l'org entière ;
2. `limit` est borné, une saisie folle retombe sur le défaut ;
3. `guide` est le nom servi de la procédure (la colonne d'hier s'appelle `doctrine`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from oto_mcp.capabilities import run_thread as RT
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES


def _espion(monkeypatch, rows=()):
    vu = {}

    def _recent(sub, org_id, limit=5):
        vu.update(sub=sub, org_id=org_id, limit=limit)
        return list(rows)
    monkeypatch.setattr(RT.db, "recent_runs", _recent)
    return vu


def test_lit_les_runs_de_lappelant_sous_lorg_active(monkeypatch):
    vu = _espion(monkeypatch)
    RT._mes_runs(ResolvedCtx(sub="alice", org_id=35), RT.MesRunsInput())
    assert vu == {"sub": "alice", "org_id": 35, "limit": 20}


def test_limit_borne(monkeypatch):
    assert RT.MesRunsInput(limit=10_000).limit == RT._MES_RUNS_MAX
    assert RT.MesRunsInput(limit=0).limit == 1
    assert RT.MesRunsInput(limit="abc").limit == 20


def test_forme_servie(monkeypatch):
    debut = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    _espion(monkeypatch, [{"run_id": "r1", "label": "relances", "guide": "relances",
                           "outcome": "done", "project_id": 7,
                           "started_at": debut, "finished_at": None}])
    out = RT._mes_runs(ResolvedCtx(sub="alice", org_id=35), RT.MesRunsInput())
    assert out == {"runs": [{"run_id": "r1", "label": "relances", "guide": "relances",
                             "outcome": "done", "project_id": 7,
                             "started_at": debut.isoformat(), "finished_at": None}]}
    RT.MesRuns(**out)


def test_route_membre_en_get():
    (cap,) = [c for c in CAPABILITIES if c.key == "runs.mine"]
    assert (cap.rest.verb, cap.rest.path) == ("GET", "/api/me/runs")
    assert cap.authz is RT.ORG_MEMBER
    assert cap.mcp is None

"""`resume_des_declencheurs` (ce que la page Agents trace) sur un VRAI PostgreSQL.

La clé `trigger_id` vit dans `payload`, un JSON libre : elle est lue en TEXTE et
convertie côté Python, jamais castée en SQL (`test_runs_surface`). Ce banc prouve que
la requête tient quand l'org porte une ligne dont la clé n'est pas un nombre — le cas
qui ferait tomber la lecture ENTIÈRE si la clé était castée — et que l'appelant reçoit
bien des entiers.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def base(pg_module_dsn):
    psycopg = pytest.importorskip("psycopg")
    url_prec = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        with psycopg.connect(pg_module_dsn, autocommit=True) as c:
            c.execute("DELETE FROM runner_jobs")
            for payload, statut, erreur in (
                ('{"trigger_id": "5"}', "done", None),
                ('{"trigger_id": "5"}', "failed", "boom"),
                ('{"trigger_id": "7"}', "done", None),
                ('{"trigger_id": "pas-un-nombre"}', "failed", "x"),
                ("{}", "done", None),
            ):
                c.execute(
                    "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, "
                    "max_attempts, payload, last_error) "
                    "VALUES (1, 'start', %s, NOW() - interval '1 hour', 1, 3, %s::jsonb, %s)",
                    (statut, payload, erreur))
        yield
    finally:
        if url_prec is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_prec


def test_le_resume_lit_la_cle_en_texte_et_rend_des_entiers(base):
    from oto_mcp.db import runner_jobs as RJ
    cases, pannes = RJ.resume_des_declencheurs(1, 7)
    assert {(c["trigger_id"], c["status"], c["n"]) for c in cases} == {
        (5, "done", 1), (5, "failed", 1), (7, "done", 1)}
    assert all(isinstance(c["trigger_id"], int) for c in cases)
    assert [(p["trigger_id"], p["last_error"]) for p in pannes] == [(5, "boom")]

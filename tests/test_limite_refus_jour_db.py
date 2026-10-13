"""Le palmarès des refus contre une VRAIE base (#1232, lot 1) : l'upsert qui cumule,
la lecture du top-N par (jour, environnement, limiteur), la purge.

⚠️ Base NEUVE par module (`pg_module_dsn`), comme `test_transport_refusals_db.py`."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def base(pg_module_dsn):
    pytest.importorskip("psycopg")
    avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = avant


def _ligne(jour, cle, refus, limiteur="debit", classe="anonyme", env="production"):
    t = datetime.combine(jour, datetime.min.time(), tzinfo=timezone.utc)
    return {"jour": jour, "env": env, "limiteur": limiteur, "classe": classe,
            "cle": cle, "refus": refus, "mode": "report", "premier_at": t,
            "dernier_at": t + timedelta(seconds=refus)}


def test_le_top_cumule_classe_et_purge(base):
    from oto_mcp import db
    from oto_mcp.db import limite_refus_jour as db_refus
    from oto_mcp.limite_refus_top import TOP_N

    aujourdhui = datetime.now(timezone.utc).date()
    # Deux minutes closes, deux processus : la même clé se cumule.
    db_refus.ajouter([_ligne(aujourdhui, "k-gros", 5), _ligne(aujourdhui, "k-petit", 1)])
    db_refus.ajouter([_ligne(aujourdhui, "k-gros", 7)])
    db_refus.ajouter([_ligne(aujourdhui, f"k-{i:02d}", 2) for i in range(TOP_N + 3)])
    db_refus.ajouter([_ligne(aujourdhui, "org-h", 4, limiteur="platform_rate",
                             classe="serper")])
    db_refus.ajouter([_ligne(aujourdhui - timedelta(days=40), "k-vieux", 9)])

    out = db.transport_refusal_stats(since_days=7)["rate_limit_top"]
    debit = [r for r in out if r["limiteur"] == "debit"]
    assert len(debit) == TOP_N, "les N premières clés par (jour, env, limiteur)"
    assert (debit[0]["cle"], int(debit[0]["refus"]), int(debit[0]["rang"])) == (
        "k-gros", 12, 1)
    assert "k-petit" not in {r["cle"] for r in debit}
    assert [(r["classe"], r["cle"], int(r["refus"])) for r in out
            if r["limiteur"] == "platform_rate"] == [("serper", "org-h", 4)]
    assert "k-vieux" not in {r["cle"] for r in out}, "hors de la fenêtre demandée"

    db_refus.purger(30)
    restantes = db.transport_refusal_stats(since_days=365)["rate_limit_top"]
    assert "k-vieux" not in {r["cle"] for r in restantes}
    assert "k-gros" in {r["cle"] for r in restantes}

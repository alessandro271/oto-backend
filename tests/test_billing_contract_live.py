"""L'abonnement réglé HORS PLATEFORME (contrat, virement) déjà en base, sur une vraie base.

Depuis la coupure du cœur (#1097), un contrat ne se déclare ni ne se résilie plus ici
(`billing_moved`, cf. `test_coupure_du_coeur.py`) et ne pose aucun droit : oto-commerce
les tient. Un contrat déjà en base reste lu. Ce que ces bancs tiennent :
- le runner ne le prélève jamais, et il n'ouvre aucun droit ;
- la révision `0008_billing_contracts` pose la table, se défait, et le boot la sait posée.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from oto_mcp import billing_runner
from oto_mcp.db import billing as db_billing
from oto_mcp.db._conn import _connect

MAINTENANT = datetime.now(timezone.utc).replace(microsecond=0)
DANS_UN_AN = MAINTENANT + timedelta(days=365)


def _org() -> int:
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (f"org-{uuid.uuid4().hex[:8]}",)).fetchone()["id"]


def _contrat_en_base(org: int) -> None:
    db_billing.set_contract_subscription(
        org, plan="standard", seats=12, unit_amount=None, currency="eur",
        interval="month", starts_at=MAINTENANT, ends_at=DANS_UN_AN, reference=None,
        granted_by="admin-test")


def test_le_runner_ne_prelève_jamais_un_contrat(live, monkeypatch):
    org = _org()
    _contrat_en_base(org)
    assert org not in {s["org_id"] for s in db_billing.due_subscriptions(limit=500)}
    monkeypatch.setattr(billing_runner.mollie_client, "create_recurring_payment",
                        lambda *a, **k: pytest.fail("un contrat ne se prélève pas"))
    ligne = db_billing.get_org_subscription(org)
    assert billing_runner._charge_one(ligne, MAINTENANT) == "skipped"


def test_un_contrat_en_base_ne_pose_aucun_droit(live):
    org = _org()
    _contrat_en_base(org)
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM org_entitlements WHERE org_id = %s",
                            (org,)).fetchone()["n"] == 0


# ── la révision ──────────────────────────────────────────────────────────────

RACINE = Path(__file__).resolve().parent.parent


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _a_la_table(dsn: str) -> bool:
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass('billing_contracts') IS NOT NULL"
                         ).fetchone()[0]


def test_la_revision_pose_la_table_se_defait_et_le_boot_la_sait_posee(live, pg_module_dsn):
    import psycopg
    from alembic import command

    from oto_mcp.db import init_db
    assert _a_la_table(pg_module_dsn), "une base NEUVE la reçoit du démarrage"
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE billing_contracts")
    cfg = _alembic()
    command.stamp(cfg, "0007_jetons_revocation_tracee")
    command.upgrade(cfg, "0008_billing_contracts")
    assert _a_la_table(pg_module_dsn), "la révision n'a rien écrit"
    command.downgrade(cfg, "0007_jetons_revocation_tracee")
    assert not _a_la_table(pg_module_dsn), "le retour arrière n'a rien retiré"
    command.upgrade(cfg, "head")
    init_db()
    assert _a_la_table(pg_module_dsn)

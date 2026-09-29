"""Billing — 4 plans (Alexis 06/07) + abonnement offert (comp, non payé, jamais tiré)
+ levée de quota (fin des credits d'appel). ADR 0043."""
from __future__ import annotations

import pytest

from oto_mcp import access, billing, billing_runner
from oto_mcp.db import billing as db_billing


# ── catalogue des 4 plans ────────────────────────────────────────────────────

def test_four_plans_with_prices():
    p = {x["plan"]: x for x in billing.plans()}
    assert set(p) == {"standard", "premium", "business", "enterprise"}
    # 19 / 99 / 249 / 499 € (Alexis 2026-08-03 ; 2ᵉ palier 49 → 99 le 2026-08-29, #490)
    assert (p["standard"]["amount"], p["premium"]["amount"],
            p["business"]["amount"], p["enterprise"]["amount"]) \
        == (1900, 9900, 24900, 49900)


def test_self_serve_refuses_custom_plan(monkeypatch):
    # aucun plan n'est `custom` aujourd'hui (4 paliers self-serve à prix fixe) ;
    # le garde-fou reste, testé sur un plan custom injecté.
    monkeypatch.setitem(billing.PLANS, "devis", {
        "label": "Devis", "amount": None, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
        "custom": True})
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: None)
    with pytest.raises(ValueError, match="custom_plan"):
        # `sub` est obligatoire depuis #487, mais le palier sur devis est refusé
        # AVANT tout préalable — le catalogue tranche en premier.
        billing.subscribe(42, "devis", "https://oto.cx/billing", sub="u-1")


def test_plan_carries_no_cap_and_unmetered():
    # modèle simplifié : aucun palier ne porte de nombre de sièges (None = pas
    # d'avis, surtout pas illimité — #805), tous unmetered.
    assert billing.plan_is_unmetered("business") is True
    assert billing.PLANS["premium"]["unipile_accounts"] is None
    assert billing.PLANS["enterprise"]["unipile_accounts"] is None


# ── abonnement offert (comp) : le runner ne le tire jamais ─────────────────
# (Offrir un plan est refusé depuis la coupure du cœur, #1097 — `billing_moved`,
# cf. `test_coupure_du_coeur.py` ; un comp déjà en base reste lu.)

def test_runner_never_charges_comp(monkeypatch):
    charged = {}
    monkeypatch.setattr(billing_runner.mollie_client, "create_recurring_payment",
                        lambda *a, **k: charged.setdefault("hit", True))
    sub = {"org_id": 7, "provider": "comp", "plan": "business", "method": "comp",
           "status": "active", "current_period_end": None}
    from datetime import datetime, timezone
    assert billing_runner._charge_one(sub, datetime(2026, 7, 6, tzinfo=timezone.utc)) == "skipped"
    assert "hit" not in charged                 # jamais de PSP derrière un comp


# ── levée de quota (fin des credits d'appel) ─────────────────────────────────

def test_unmetered_org_bypasses_quota(monkeypatch):
    # Le plan pose le droit `platform_unmetered` ; le cœur ne lit que ce droit.
    assert access.PLATFORM_UNMETERED in billing.plan_rights("premium")


def test_no_plan_org_keeps_quota(monkeypatch):
    assert billing.plan_rights("inconnu") == ()

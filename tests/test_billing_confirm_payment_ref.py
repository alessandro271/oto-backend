"""Le paiement encaissé doit être CELUI qu'on confirme (#291).

Le scénario qui perdait de l'argent : rien n'interdit deux souscriptions ouvertes à
la fois (retour arrière, page rechargée, hésitation carte/SEPA), donc deux pages
payables. Le payeur termine l'ANCIENNE ; Mollie encaisse ; le webhook retrouve la
bonne ligne ; puis `confirm` repartait « du plus récent », la trouvait non payée et
rendait `pending`. Résultat : org débitée, aucun droit ouvert, et un journal qui
annonçait `confirmed`.

La facturation est en production : ces tests exercent la logique réelle avec le PSP
et la base stubbés, jamais le seam qu'ils vérifient.

Depuis la coupure du cœur (#1097), un encaissement constaté est gravé sur le BON
paiement, puis refusé (`billing_moved`) sans ouvrir d'abonnement : ce que ces bancs
tiennent encore, c'est que le paiement nommé soit celui qu'on grave et qu'on nomme.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import billing


TERMINAL = ("failed", "canceled", "expired", "paid_confirmed")


class _Db:
    """Journal de paiements en mémoire, trié DESC comme la vraie requête."""

    def __init__(self, payments, sub=None):
        self.payments = payments
        self.sub = sub
        self.upserted = []
        self.updated = []

    def list_billing_payments(self, org_id, limit=20):
        return self.payments[:limit]

    def get_org_subscription(self, org_id):
        return self.sub

    def update_billing_payment(self, pid, **kw):
        self.updated.append((pid, kw))

    def upsert_org_subscription(self, org_id, **kw):
        self.upserted.append((org_id, kw))

    def get_billing_payment_by_ref(self, ref):
        return next((p for p in self.payments if p["payment_intent_id"] == ref), None)

    TERMINAL_PAYMENT_STATUSES = TERMINAL


@pytest.fixture(autouse=True)
def _trace_sans_base(monkeypatch):
    from oto_mcp import billing_invoices
    monkeypatch.setattr(billing_invoices, "tracer_encaissement", lambda rid: None)


@pytest.fixture(autouse=True)
def _production(monkeypatch):
    """Ces épreuves jouent la PRODUCTION, où un paiement `live` ouvre un droit ; hors
    d'elle aucun n'en ouvre (`billing_mode`, 10/09/2026)."""
    monkeypatch.setenv("OTO_ENV", "prod")
    monkeypatch.delenv("OTO_SENTRY_ENV", raising=False)


def _payment(pid, ref, status="open", age=timedelta(seconds=2)):
    return {"id": pid, "payment_intent_id": ref, "kind": "initial",
            "status": status, "org_id": 7,
            "created_at": datetime.now(timezone.utc) - age}


@pytest.fixture
def deux_checkouts(monkeypatch):
    """Deux paiements ouverts ; SEUL l'ancien a été encaissé chez le PSP."""
    db = _Db([_payment(2, "tr_RECENT"), _payment(1, "tr_ANCIEN")])
    monkeypatch.setattr(billing, "db_billing", db)

    def get_payment(ref):
        if ref == "tr_ANCIEN":
            return {"id": ref, "mode": "live", "status": "paid", "customerId": "cst_1",
                    "metadata": {"plan": "standard"}}
        return {"id": ref, "mode": "live", "status": "open"}

    monkeypatch.setattr(billing.mollie_client, "get_payment", get_payment)
    return db


def test_sans_reference_on_confirme_le_mauvais(deux_checkouts):
    """Le comportement d'origine, conservé pour le POLLING qui ne sait pas lequel a
    été payé : il regarde le plus récent, non payé, et rend `pending`. Ce test fige
    la raison d'être du paramètre — sans lui, l'encaissement reste invisible."""
    out = billing.confirm(7)
    assert out["status"] == "pending"
    assert deux_checkouts.upserted == [], "aucun abonnement ne doit être posé"


def test_avec_la_reference_le_bon_paiement_est_grave_puis_refuse(deux_checkouts):
    """Le webhook sait lequel a été payé, il le dit : c'est CELUI-LÀ qui est gravé
    `paid` et nommé dans le refus — et aucun abonnement ne s'ouvre (#1097)."""
    with pytest.raises(ValueError, match="billing_moved: paiement tr_ANCIEN"):
        billing.confirm(7, payment_ref="tr_ANCIEN")
    assert deux_checkouts.updated == [(1, {"status": "paid", "payment_id": "tr_ANCIEN"})]
    assert deux_checkouts.upserted == []


def test_le_webhook_transmet_la_reference(monkeypatch, deux_checkouts):
    """Bout en bout : le webhook passe la référence, l'encaissement est gravé sur la
    bonne ligne, et l'issue dit la vérité — rien n'a été confirmé."""
    assert billing.process_webhook("tr_ANCIEN") == "not_confirmed"
    assert deux_checkouts.updated == [(1, {"status": "paid", "payment_id": "tr_ANCIEN"})]
    assert deux_checkouts.upserted == []


def test_une_reference_inconnue_ne_se_rabat_sur_personne(deux_checkouts):
    """Se rabattre sur un autre paiement reviendrait à ouvrir des droits sur la foi
    d'un encaissement qui concerne autre chose."""
    with pytest.raises(ValueError) as e:
        billing.confirm(7, payment_ref="tr_INCONNU")
    assert "unknown_payment" in str(e.value)
    assert deux_checkouts.upserted == []


def test_le_webhook_n_annonce_pas_un_succes_qu_il_n_a_pas_constate(monkeypatch):
    """`confirmed` était rendu quoi qu'il arrive : le journal affirmait le contraire
    de ce qui s'était passé, ce qui envoie chercher l'incident ailleurs."""
    vieux = _payment(1, "tr_X")
    vieux["created_at"] = (datetime.now(timezone.utc)
                           - billing.PENDING_WINDOW - timedelta(minutes=1))
    db = _Db([vieux])
    monkeypatch.setattr(billing, "db_billing", db)
    monkeypatch.setattr(billing.mollie_client, "get_payment",
                        lambda ref: {"id": ref, "mode": "live", "status": "paid", "customerId": "cst_1",
                                     "metadata": {"plan": "standard"}})
    assert billing.process_webhook("tr_X") == "not_confirmed"
    assert db.upserted == []


def test_un_initial_ouvert_ancien_reste_visible(monkeypatch):
    """Le défaut aggravant : au `limit` par défaut (20), un paiement ouvert plus
    ancien sortait de la fenêtre et n'était jamais confirmé, sans aucun message."""
    vieux = _payment(1, "tr_VIEUX")
    db = _Db([_payment(i, f"tr_{i}", status="expired") for i in range(50, 1, -1)] + [vieux])
    monkeypatch.setattr(billing, "db_billing", db)
    monkeypatch.setattr(billing.mollie_client, "get_payment",
                        lambda ref: {"id": ref, "mode": "live", "status": "paid", "customerId": "c",
                                     "metadata": {"plan": "standard"}})
    with pytest.raises(ValueError, match="billing_moved: paiement tr_VIEUX"):
        billing.confirm(7, payment_ref="tr_VIEUX")
    assert db.updated == [(1, {"status": "paid", "payment_id": "tr_VIEUX"})]

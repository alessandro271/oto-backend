"""Facturation des abonnements (#488) — la TRACE d'un encaissement.

⚠️ **Plus rien n'est créé chez Pennylane par la plateforme depuis le 2026-09-09**
(décision d'Alexis) : ni facture, ni avoir, ni fiche client. Le seam fournisseur
`pennylane.py` et le chemin d'émission qui l'appelait ont été **retirés**, pas
neutralisés — `emission.py` dit pourquoi, et quelle décision reste à prendre.

Ce paquet ne porte donc plus qu'une responsabilité : **tout encaissement a sa
ligne**, en attente d'une pièce posée à la main.

    emission.py    le CYCLE — trace, attente, reprise

⚠️ **Aucun e-mail ne part de ce paquet** depuis le 2026-09-09 non plus : la
plateforme n'envoie plus de facture par courrier, elle la met à disposition. Le
`mail.py` qui portait cet envoi a été retiré, pas neutralisé.

Ce qui vit ailleurs, et pourquoi :

- la **table** `billing_invoices` → `db/billing_invoices.py`, comme tout le store.
  Elle garde son vocabulaire complet (`issued`, numéro, PDF) : ce sont les
  documents d'avant la coupure, et ce dont la reprise en main aura besoin ;
- la **surface** (liste + PDF) → `capabilities/billing_invoices.py` et la route de
  téléchargement dans `api/billing.py` (un PDF ne passe pas par la couche
  capacité, qui ne rend que du JSON).

Ce que le reste du backend appelle tient en deux noms — les appelants
(`billing.confirm`, `billing.process_webhook`, `billing_runner.tick`) ne
connaissent pas la table.
"""
from __future__ import annotations

from .emission import (          # noqa: F401 — la façade EST la surface publique
    EN_ATTENTE_DE_LA_MAIN,
    ensure_credit_note_for_refund,
    ensure_invoice_for_payment,
    sweep,
    tracer_encaissement,
    tracer_remboursement,
)

__all__ = [
    "EN_ATTENTE_DE_LA_MAIN",
    "ensure_credit_note_for_refund",
    "ensure_invoice_for_payment",
    "sweep",
    "tracer_encaissement",
    "tracer_remboursement",
]

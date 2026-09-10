"""Un paiement n'ouvre de droit que s'il est RÉEL et constaté par la PRODUCTION.

Relevé le 10/09/2026 : la préproduction expose la souscription avec la clé Mollie de
TEST, sur la base qu'elle partage avec la production, et `confirm` ne regardait pas le
mode du paiement reçu. Un paiement de test y ouvrait donc un droit — et ce droit, écrit
dans la base partagée, est un droit de production : l'org servie, l'option débloquée,
sans un centime encaissé.

La règle, dans les deux sens :
- en production, un paiement de TEST n'ouvre aucun droit — une clé de test posée par
  erreur en production ne produirait plus que des refus, jamais des abonnements offerts ;
- hors production, AUCUN paiement n'en ouvre : la préproduction partage la base, un
  droit qu'elle ouvrirait serait un droit réel ; et un paiement réel n'a rien à y faire.

Le mode se lit dans la réponse du PRESTATAIRE (`mode` : `live` | `test`), jamais dans une
variable locale (le préfixe de la clé, un réglage) : c'est le seul témoin de ce qui a été
encaissé. L'environnement vient de `config.est_la_production()`, qui refuse de deviner :
un environnement ambigu refuse ici aussi.

Passent par ici tous les constats de paiement qui ouvrent ou déplacent un droit :
`billing.confirm` (souscription — retour navigateur, webhook, rattrapage du runner) et
`billing_method.confirm` (changement de moyen : un mandat de test posé sur un abonnement
réel ferait échouer le prélèvement suivant).
"""
from __future__ import annotations

from . import config

LIVE = "live"


def exiger_paiement_reel(payment: dict) -> None:
    """Lève si ce paiement ne peut ouvrir aucun droit ici. À appeler AVANT toute écriture
    tirée de ce paiement (journal, trace de facture, miroir, mandat)."""
    mode = payment.get("mode")
    if not config.est_la_production():
        raise RuntimeError(
            "billing_not_production: cette instance n'est pas la production ; elle "
            "partage sa base, où tout droit ouvert est un droit réel. Aucun paiement "
            f"n'y ouvre de droit (paiement {payment.get('id')!r}, mode {mode!r}).")
    if mode != LIVE:
        raise RuntimeError(
            f"payment_mode_mismatch: le paiement {payment.get('id')!r} est en mode "
            f"{mode!r} — la production n'ouvre de droit que sur un paiement réel "
            "(`live`). Une clé de test serait-elle posée en production ?")

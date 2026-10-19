"""La boucle des ÉCHÉANCES du budget d'unités (`access/credits.echoir`).

Une réservation reste ouverte jusqu'à son règlement. Celle dont l'appel n'a jamais été
réglé — un processus arrêté en plein appel, un travail asynchrone dont personne n'a
relevé le résultat — se règle à son échéance, au montant réservé, marquée
`hold_expired` : le disponible du payeur ne reste pas retenu.

Prod et préprod partagent la base : deux boucles peuvent voir la même échéance, une
seule la règle (`credit_holds.state = 'open'` dans l'écriture même). Aucun effet chez un
tiers.
"""
from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_POLL_S = 60
# Au plus autant de réservations réglées par tour ; un tour plein enchaîne sans attendre.
_LOT = 100


async def run_credits_echeances_loop(interval: int = _POLL_S) -> None:
    from .access import credits as budget
    logger.info("credits_echeances: started (poll %ss).", interval)
    while True:
        try:
            if await run_in_threadpool(budget.echoir, _LOT) >= _LOT:
                continue
        except Exception as e:  # noqa: BLE001 — a failed tick doesn't kill the loop
            logger.warning("credits_echeances: tick failed: %s", e)
        await asyncio.sleep(interval)

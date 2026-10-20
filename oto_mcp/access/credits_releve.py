"""The collection of an asynchronous job, under the units budget (`access/credits`).

A submission's hold stays open until its collection (`cost.collection_of`, kept on the
hold's `job`): the collection that finds the job FINISHED settles it at what was found,
unit by unit (`regler_releve`); the one that learns no result will come releases it
(`liberer_releve`); otherwise its deadline settles it at the amount reserved
(`credits_echeances`). Called by the collecting tool, on the customer's key as on the
platform's: a collection no hold awaits is a no-op.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from ..db import credits as db_credits
from . import credits as budget

logger = logging.getLogger(__name__)


def regler_releve(function: str, args: dict, quantites: Optional[dict]) -> bool:
    """The collection `function(args)` of an asynchronous job came back FINISHED: settles
    the hold that awaited it at what the collection read, `{unit: quantity}`, each unit
    of the hold under its line of the hold's scale version (a unit read that the hold
    did not reserve is not settled: it was not asked). `None` = no figure to read: at
    the amount reserved. Returns False when no hold awaited this collection (the
    customer's own key, already settled, expired). Never raises: the read was served."""
    try:
        hold = db_credits.reservation_de_la_releve(function, args)
        if hold is None:
            return False
        if quantites is None:
            db_credits.regler(hold["call_uid"], montant=None, quantite=None,
                              meta={"from": "unreadable", "exceeded": False})
            return True
        lignes = _lignes_de(hold)
        lus = {u: quantites.get(u, 0) for u in lignes}
        if any(isinstance(q, bool) or not isinstance(q, (int, float))
               or not math.isfinite(q) or q < 0 for q in lus.values()):
            raise ValueError(f"unreadable quantities {lus}")
        montant = sum(budget._paquets(q, *lignes[u]) for u, q in lus.items())
        depasse = montant > int(hold["reserved"])
        if db_credits.regler(hold["call_uid"], montant=montant,
                             quantite=int(math.ceil(sum(lus.values()))),
                             meta={"from": "response", "exceeded": depasse,
                                   "units": lus}) is None:
            budget._apres_echeance(hold["call_uid"], montant)
        return True
    except Exception:  # noqa: BLE001 — the hold stays open; its deadline settles it
        logger.warning("crédits : relève %s %s non réglée (la réservation reste ouverte : "
                       "son échéance la règle)", function, args, exc_info=True)
        return False


def liberer_releve(function: str, args: dict) -> bool:
    """The asynchronous job ended WITHOUT a result (the provider says so): the hold that
    awaited its collection is released. Never raises."""
    try:
        hold = db_credits.reservation_de_la_releve(function, args)
        return hold is not None and db_credits.liberer(hold["call_uid"]) is not None
    except Exception:  # noqa: BLE001 — the hold stays open; its deadline settles it
        logger.warning("crédits : relève %s %s non libérée", function, args, exc_info=True)
        return False


def _lignes_de(hold: dict) -> dict[str, tuple[int, int]]:
    """The scale lines the hold was reserved under (its version, not today's), one per
    unit of the hold. Raises if one is missing."""
    unites = hold["unit"].split("+")
    lignes = {l["unit"]: (int(l["budget"]), int(l["per_n"]))
              for l in db_credits.lignes_du_bareme(int(hold["scale_version"]))
              if (l["connector"], l["function"]) == (hold["connector"], hold["function"])
              and l["unit"] in unites}
    if len(lignes) != len(unites):
        raise LookupError(f"credit scale v{hold['scale_version']} lacks a line of "
                          f"{hold['connector']}.{hold['function']} ({hold['unit']})")
    return lignes

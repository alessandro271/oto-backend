"""Model calls of a hosted agent on a SHARED key, under the units budget.

Not a tool: the runner worker calls the model itself, on a key the platform or the
tenant pays (`capabilities/runner_jobs`). Each call is a TURN: reserved before it is
sent, at its `max_tokens` of output — the only bound known before the call; the input
and the cache are not reserved (no token count before the call) and settle as
`exceeded` — then settled at the four token counts the provider answered, under the
scale lines of `anthropic.messages`, one per unit. The mode, the scale and the payer
are the budget's (`access/credits`).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .. import limite_refus_top
from ..db import credits as db_credits
from . import credits as budget

logger = logging.getLogger(__name__)

MODELE_CONNECTOR, MODELE_FUNCTION = "anthropic", "messages"
#: The units of a model call, under the provider's own usage names.
UNITES_DU_MODELE = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens")
_UNITE_RESERVEE = "output_tokens"


class TourRefuse(Exception):
    """A turn the ledger refuses (`enforce`): `code` is one of the named refusals, and
    `retryable` says whether the same turn may be asked again."""

    def __init__(self, code: str, message: str, retryable: bool, **data):
        super().__init__(message)
        self.code, self.retryable, self.data = code, retryable, data


def _lignes_du_modele(lignes: dict) -> Optional[dict]:
    """`{unit: (budget, per_n)}` for the four units, or None if one is missing."""
    out = {u: lignes.get((MODELE_CONNECTOR, MODELE_FUNCTION, u)) for u in UNITES_DU_MODELE}
    return None if any(v is None for v in out.values()) else out


def reserver_tour(uid: str, sub: str, org: Optional[int], max_tokens: int) -> dict:
    """Reserves one model call before it is sent: `max_tokens` of output, rounded up to
    the scale's packages. Idempotent on `uid` (a worker that asks again gets the same
    answer). Returns `{held, reserved}`; `enforce` raises `TourRefuse`, `report`
    reserves anyway (forced) and logs what it would refuse, `off` holds nothing."""
    m = budget.mode()
    if m == "off":
        return {"held": False, "reserved": 0}
    try:
        lu = budget._relire()
        lignes = _lignes_du_modele(lu["lignes"])
        if lignes is None:
            limite_refus_top.noter_credits(
                f"unpriced:{MODELE_CONNECTOR}.{MODELE_FUNCTION}",
                budget.payeur(sub, org, lu["personnes"]), m, time.time())
            logger.warning("crédits (%s) : %s.%s sans ses quatre lignes au barème v%s — %s",
                           m, MODELE_CONNECTOR, MODELE_FUNCTION, lu["version"],
                           "refusé" if m == "enforce" else "servi sans réservation")
            if m == "enforce":
                raise TourRefuse(budget.SANS_BAREME, "model calls have no price in this "
                                 "instance's credit scale yet: the shared key cannot be "
                                 "used.", False)
            return {"held": False, "reserved": 0}
        reserve = budget._paquets(max_tokens, *lignes[_UNITE_RESERVEE])
        qui = budget.payeur(sub, org, lu["personnes"])
        r = db_credits.reserver(
            call_uid=uid, payer=qui, connector=MODELE_CONNECTOR, function=MODELE_FUNCTION,
            unit=_UNITE_RESERVEE, scale_version=lu["version"], quantity=max_tokens,
            reserved=reserve, deadline_s=budget.hold_max_s(), forcer=(m == "report"))
    except TourRefuse:
        raise
    except Exception:
        logger.exception("crédits (%s) : registre indisponible pour le tour %s — %s", m,
                         uid, "refusé" if m == "enforce" else "servi")
        if m == "enforce":
            raise TourRefuse(budget.INDISPONIBLE, "The credit ledger could not be written; "
                             "the model was not called. Retry in a moment.", True) from None
        return {"held": False, "reserved": 0}
    if r.insuffisant:
        logger.warning("crédits (%s) : %s à %d disponibles pour %d réservées (tour %s) — %s",
                       m, qui, r.disponible, reserve, uid,
                       "refusé" if m == "enforce" else "servi")
        if not r.posee:
            raise TourRefuse(budget.INSUFFISANT, f"Not enough credits for this model call: "
                             f"it may cost up to {reserve}, {max(0, r.disponible)} are "
                             "available.",
                             False, needed=reserve, available=max(0, r.disponible))
    return {"held": True, "reserved": reserve}


def regler_tour(uid: str, sub: str, org: Optional[int], tokens: dict,
                meta: dict) -> Optional[dict]:
    """Settles one model call at the four token counts the provider answered. A call
    that was not reserved (a worker that skipped the reservation) is reserved now,
    forced: it has been consumed. Returns `{settled, amount, exceeded}`, `settled:
    False` when the turn was already settled (a report replayed), or None when the
    scale has no price for model calls (nothing is written, it is logged), or in `off`
    for a turn it never reserved (nothing is written: `off` only closes what is open).
    Raises when the ledger cannot be written (the caller decides, by mode)."""
    hold = db_credits.reservation(uid)
    if hold is not None and hold["state"] != db_credits.OUVERTE:
        return {"settled": False, "amount": 0, "exceeded": False}
    if hold is None and budget.mode() == "off":
        return None
    lu = budget._relire()
    # The lines of the version the turn was reserved under, not today's.
    version = lu["version"] if hold is None else int(hold["scale_version"])
    hold_lignes = _lignes_du_modele(lu["lignes"] if version == lu["version"] else {
        (l["connector"], l["function"], l["unit"]): (int(l["budget"]), int(l["per_n"]))
        for l in db_credits.lignes_du_bareme(version)})
    if hold_lignes is None:
        logger.warning("crédits : tour %s non réglé — %s.%s sans ses quatre lignes au "
                       "barème v%s", uid, MODELE_CONNECTOR, MODELE_FUNCTION, version)
        return None
    if hold is None:
        sortie = int(tokens[_UNITE_RESERVEE])
        hold = {"reserved": budget._paquets(sortie, *hold_lignes[_UNITE_RESERVEE])}
        db_credits.reserver(
            call_uid=uid, payer=budget.payeur(sub, org, lu["personnes"]),
            connector=MODELE_CONNECTOR, function=MODELE_FUNCTION, unit=_UNITE_RESERVEE,
            scale_version=version, quantity=sortie, reserved=hold["reserved"],
            deadline_s=budget.hold_max_s(), forcer=True)
    montant = sum(budget._paquets(int(tokens[u]), *hold_lignes[u])
                  for u in UNITES_DU_MODELE)
    depasse = montant > int(hold["reserved"])
    clos = db_credits.regler(uid, montant=montant, quantite=int(tokens[_UNITE_RESERVEE]),
                             meta={**meta, "from": "response", "exceeded": depasse,
                                   "tokens": {u: int(tokens[u]) for u in UNITES_DU_MODELE}})
    if clos is None:
        return {"settled": False, "amount": 0, "exceeded": False}
    return {"settled": True, "amount": montant, "exceeded": depasse}


def clore_les_tours(prefixe: str) -> int:
    """At the end of an attempt, the turns reserved and never reported (the call that
    cut the loop, a worker that died between the call and its report) settle at the
    amount reserved, marked `unreported`. Returns how many. Never raises."""
    n = 0
    try:
        for uid in db_credits.reservations_ouvertes(prefixe):
            if db_credits.regler(uid, montant=None, quantite=None,
                                 meta={"from": "unreported", "exceeded": False}):
                n += 1
    except Exception:  # noqa: BLE001 — the holds stay open; their deadline settles them
        logger.warning("crédits : tours %s* non clos (leur échéance les règle)", prefixe,
                       exc_info=True)
    if n:
        logger.warning("crédits : %d tour(s) %s* jamais rapporté(s), réglé(s) au montant "
                       "réservé", n, prefixe)
    return n

"""Les échéances du budget d'unités (`access/credits.echoir`, boucle
`credits_echeances`) : une réservation jamais réglée se règle à son échéance, au montant
réservé, marquée `hold_expired` — une fois, et jamais celle que son appel a déjà réglée.
"""
from __future__ import annotations

import uuid

from oto_mcp import boucles_de_fond
from oto_mcp.access import credits
from oto_mcp.db import credits as db_credits
from oto_mcp.db._conn import _connect


def _reserver(payeur, reserve):
    uid = uuid.uuid4().hex
    db_credits.reserver(call_uid=uid, payer=payeur, connector="pennylane",
                        function="get_quote", unit="requests", scale_version=1,
                        quantity=2, reserved=reserve, deadline_s=3600, forcer=False)
    return uid


def _echue(uid):
    with _connect() as conn:
        conn.execute("UPDATE credit_holds SET deadline = NOW() - interval '1 second' "
                     "WHERE call_uid = %s", (uid,))


def _etat(uid):
    with _connect() as conn:
        return conn.execute("SELECT state FROM credit_holds WHERE call_uid = %s",
                            (uid,)).fetchone()["state"]


def test_une_reservation_echue_se_regle_au_montant_reserve_une_fois(live):
    payeur = f"org:{uuid.uuid4().int % 10 ** 8}"
    db_credits.verser(payeur, 50, "test", uuid.uuid4().hex)
    echue, reglee, en_cours = (_reserver(payeur, 10), _reserver(payeur, 7),
                               _reserver(payeur, 3))
    db_credits.regler(reglee, montant=4, quantite=1, meta={})
    _echue(echue)
    _echue(reglee)
    assert credits.echoir() == 1
    assert credits.echoir() == 0, "une échéance se règle une fois"
    assert (_etat(echue), _etat(reglee), _etat(en_cours)) == ("expired", "settled", "open")
    s = db_credits.solde(payeur)
    assert (s["balance"], s["holds_open"]) == (50 - 4 - 10, 3)
    mouvement = [m for m in db_credits.mouvements_apres(0, 0, 1000)
                 if m["call_uid"] == echue]
    assert [(m["kind"], m["amount"], m["quantity"], m["meta"]["hold_expired"])
            for m in mouvement] == [("settle", -10, 2, True)]


def test_la_boucle_est_composee_partout_et_n_agit_sur_aucun_tiers():
    boucle = next(b for b in boucles_de_fond.BOUCLES if b.nom == "credits_echeances")
    assert boucle.tiers is False
    from oto_mcp import credits_echeances
    assert boucle.fonction() is credits_echeances.run_credits_echeances_loop

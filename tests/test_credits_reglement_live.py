"""Le stockage du budget d'unités contre une vraie base (`db/credits.py`) : ce que
réserver, régler et libérer écrivent, ce que deux réservations simultanées du même
payeur se disputent, et ce que cela coûte en requêtes sur le chemin d'un appel (#1148).

Relevé du 19/10/2026 : une réservation = une transaction de 2 écritures (la
réservation, puis le solde, verrouillé par sa ligne) ; un règlement = 3 (la
réservation close, le solde, le mouvement) ; une libération = 2. Le barème et les modes
de payeur sont relus au plus toutes les 30 s (`access/credits._relire`) : un appel ne
paie pas leur lecture. Un écart est une régression du chemin chaud, ou une économie à
graver ici.
"""
from __future__ import annotations

import threading
import uuid

import psycopg
import pytest

from _credits_horizon import mouvements_sous_l_horizon
from oto_mcp.db import credits as db_credits

_RESERVER, _REGLER, _LIBERER = 2, 3, 2


def _compter(f):
    """`(résultat, requêtes)` — chaque `execute` et chaque ligne d'un `executemany`."""
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        out = f()
    return out, len(vues)


def _payeur() -> str:
    return f"org:{uuid.uuid4().int % 10 ** 8}"


def _verser(payeur, montant):
    db_credits.verser(payeur, montant, "test", uuid.uuid4().hex)


def _reserver(payeur, reserve, forcer=False, uid=None):
    return db_credits.reserver(
        call_uid=uid or uuid.uuid4().hex, payer=payeur, connector="pennylane",
        function="get_quote", unit="requests", scale_version=1, quantity=1,
        reserved=reserve, deadline_s=60, forcer=forcer)


def _solde(payeur):
    s = db_credits.solde(payeur)
    return s["balance"], s["holds_open"]


def test_reserver_puis_regler_au_reel(live):
    p = _payeur()
    _verser(p, 100)
    uid = uuid.uuid4().hex
    r, n = _compter(lambda: _reserver(p, 30, uid=uid))
    assert (r.posee, r.insuffisant, r.disponible, n) == (True, False, 100, _RESERVER)
    assert _solde(p) == (100, 30)
    hold, n = _compter(lambda: db_credits.regler(
        uid, montant=12, quantite=4, meta={"from": "response", "exceeded": False}))
    assert hold["reserved"] == 30 and n == _REGLER
    assert _solde(p) == (88, 0), "le surplus réservé revient au solde"
    assert db_credits.regler(uid, montant=12, quantite=4, meta={}) is None, \
        "une réservation se règle une fois"
    assert _solde(p) == (88, 0)


def test_liberer_ne_consomme_rien(live):
    p = _payeur()
    _verser(p, 10)
    uid = uuid.uuid4().hex
    _reserver(p, 10, uid=uid)
    hold, n = _compter(lambda: db_credits.liberer(uid))
    assert hold["reserved"] == 10 and n == _LIBERER
    assert _solde(p) == (10, 0)
    assert db_credits.liberer(uid) is None


def test_la_reservation_est_idempotente_sur_l_appel(live):
    p = _payeur()
    _verser(p, 10)
    uid = uuid.uuid4().hex
    assert _reserver(p, 4, uid=uid).posee
    r = _reserver(p, 4, uid=uid)
    assert (r.posee, r.deja) == (False, True)
    assert _solde(p) == (10, 4)


def test_en_application_rien_n_est_retenu_au_dela_du_disponible(live):
    p = _payeur()
    _verser(p, 5)
    uid = uuid.uuid4().hex
    r = _reserver(p, 6, uid=uid)
    assert (r.posee, r.insuffisant, r.disponible) == (False, True, 5)
    assert _solde(p) == (5, 0)
    assert db_credits.liberer(uid) is None, "aucune réservation n'a été écrite"


def test_en_mesure_la_reservation_est_posee_et_le_dit(live):
    p = _payeur()
    r = _reserver(p, 6, forcer=True)
    assert (r.posee, r.insuffisant, r.disponible) == (True, True, 0)
    assert _solde(p) == (0, 6)


def test_un_reglement_au_dela_du_pire_cas_rend_le_solde_negatif(live):
    p = _payeur()
    _verser(p, 10)
    uid = uuid.uuid4().hex
    _reserver(p, 10, uid=uid)
    db_credits.regler(uid, montant=15, quantite=15,
                      meta={"from": "response", "exceeded": True})
    assert _solde(p) == (-5, 0)


def test_deux_reservations_simultanees_du_meme_payeur_n_engagent_que_le_disponible(live):
    """Le verrou de la ligne du payeur sérialise : 10 réservations de 3 sur un solde de
    10, lancées ensemble, en posent exactement 3."""
    p = _payeur()
    _verser(p, 10)
    issues, depart = [], threading.Barrier(10)

    def _une():
        depart.wait()
        issues.append(_reserver(p, 3).posee)
    fils = [threading.Thread(target=_une) for _ in range(10)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert issues.count(True) == 3
    assert _solde(p) == (10, 9)


def test_la_releve_d_un_travail_asynchrone_retrouve_sa_reservation(live):
    p = _payeur()
    _verser(p, 50)
    uid = uuid.uuid4().hex
    _reserver(p, 20, uid=uid)
    job = {"function": "fullenrich.fetch", "args": {"enrichment_id": uid},
           "within_s": None, "no_deadline": True}
    assert db_credits.attendre_la_releve(uid, job, 3600)
    db_credits.regler(uid, montant=20, quantite=1, meta={})
    assert not db_credits.attendre_la_releve(uid, job, 3600), "réglée : plus de relève"


def test_le_reglement_se_lit_au_curseur(live):
    p = _payeur()
    _verser(p, 10)
    uid = uuid.uuid4().hex
    _reserver(p, 4, uid=uid)
    db_credits.regler(uid, montant=3, quantite=3,
                      meta={"from": "response", "exceeded": False})
    lus = mouvements_sous_l_horizon(lambda m: m["payer"] == p, 2)
    assert [(m["kind"], m["amount"], m["held"]) for m in lus] == [
        ("deposit", 10, 0), ("settle", -3, -4)]
    assert lus[1]["meta"] == {"from": "response", "exceeded": False, "scale_version": 1}
    assert lus[1]["call_uid"] == uid

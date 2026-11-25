"""L'instant « révoqué avant » sur SQL réel (`db/revocations_comptes.py`), sa lecture par la
garde (`db.get_suspension`), la date d'un jeton `oto_` (`db.verify_api_token`) et la garde
de bout en bout. Puis la pose d'amorce des empreintes (`db/empreintes_2fa.py`). Patron de
base éphémère : `test_empreintes_2fa_db_live.py::live`."""
from __future__ import annotations

import os
import time
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = avant


def _sub() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"


def test_poser_ne_recule_jamais(live):
    from oto_mcp.db import _connect, revocations_comptes
    sub = _sub()
    assert revocations_comptes.lire(sub) is None
    premier = revocations_comptes.poser(sub, "mot de passe changé (PostResetPassword)")
    second = revocations_comptes.poser(sub, "double authentification changée (PostSignIn)")
    assert second >= premier
    assert isinstance(premier, float), "des secondes, pas une date tronquée à la seconde"
    # Un instant plus récent écrit à la main n'est pas reculé par une pose.
    with _connect() as conn:
        conn.execute("UPDATE revocations_comptes SET revoque_avant = "
                     "revoque_avant + interval '1 hour' WHERE sub = %s", (sub,))
    futur = revocations_comptes.lire(sub)
    assert revocations_comptes.poser(sub, "remise à zéro") == futur


def test_la_lecture_de_la_garde_porte_l_instant_sans_ligne_users(live):
    """Le webhook nomme un compte de l'annuaire, qui n'a pas forcément de ligne `users`."""
    from oto_mcp import db
    from oto_mcp.db import revocations_comptes
    sub = _sub()
    assert db.get_suspension(sub) is None
    pose = revocations_comptes.poser(sub, "test")
    entree = db.get_suspension(sub)
    assert entree["sub"] == sub and entree["suspended_at"] is None
    assert entree["revoque_avant"] == pose


def test_la_lecture_de_la_garde_porte_pause_et_instant(live):
    from oto_mcp import db
    from oto_mcp.db import revocations_comptes
    sub = _sub()
    db.upsert_user(sub)
    assert db.get_suspension(sub) is None, "compte vivant, jamais fermé"
    db.suspend_account(sub, by="op", reason="test")
    assert db.get_suspension(sub)["revoque_avant"] is None
    revocations_comptes.poser(sub, "test")
    entree = db.get_suspension(sub)
    assert entree["suspended_at"] is not None and entree["revoque_avant"] is not None


def test_un_jeton_oto_cree_avant_la_fermeture_est_refuse_par_la_garde(live):
    from oto_mcp import db, garde_identite
    from oto_mcp.db import revocations_comptes
    sub = _sub()
    ancien = db.create_api_token(sub, "ancien")
    time.sleep(0.01)
    revocations_comptes.poser(sub, "test")
    time.sleep(0.01)
    neuf = db.create_api_token(sub, "neuf")

    ligne = db.verify_api_token(ancien)
    assert isinstance(ligne["cree_le"], float)
    assert garde_identite.refus(sub, ligne["cree_le"])[0] == "session_revoked"
    ligne = db.verify_api_token(neuf)
    assert garde_identite.refus(sub, ligne["cree_le"]) is None


def test_comptes_sans_empreinte_et_pose_si_absente(live):
    from oto_mcp import db
    from oto_mcp.db import empreintes_2fa
    prefixe = f"p{uuid.uuid4().hex[:6]}-"
    a, b, c = (prefixe + x for x in "abc")
    for s in (a, b, c, f"acme:{prefixe}x"):
        db.upsert_user(s)
    empreintes_2fa.poser(b, "b" * 64)
    trouves = [s for s in empreintes_2fa.comptes_sans_empreinte(prefixe, 1000)
               if s.startswith(prefixe)]
    assert trouves == [a, c], "un compte qualifié (`:`) ou déjà posé n'y est pas"
    assert empreintes_2fa.poser_si_absente(a, "a" * 64) is True
    assert empreintes_2fa.poser_si_absente(a, "z" * 64) is False
    assert empreintes_2fa.lire(a) == "a" * 64, "jamais écrasée"
    assert a not in empreintes_2fa.comptes_sans_empreinte(prefixe, 1000)

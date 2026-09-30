"""oto#230 — le run d'un bail : renouveler ne le change pas, libérer l'efface.

`claimed_run` est la seule preuve de titularité en écriture (`_lease_guard`), alors
que `worker` est un libellé que l'appelant choisit. Deux défauts relevés à la lecture :

1. la libération ligne à ligne gardait `claimed_run`, et la fermeture du run la
   touchait ensuite une seconde fois — une révision qui avance sans rien de visible
   (corrigé par #664, figé ici sous l'angle de la révision) ;
2. un renouvellement par `claim_row` du même libellé RÉÉCRIVAIT `claimed_run` : hors
   run, la ligne se détachait du run qui la tenait (elle ne se libérait plus à sa
   fermeture, et son titulaire se voyait refuser l'écriture) ; sous un autre run, elle
   lui était rattachée sur la seule foi du libellé.

La règle : une PRISE (ligne libre, ou bail échu) pose le run de l'appel ; un
renouvellement garde celui du bail qu'il prolonge. Changer de run, c'est libérer puis
réserver.

Le troisième point de l'issue (supprimer par REST une ligne sous bail) est figé par
`test_preconditions_de_revision_217.py::test_supprimer_une_ligne_sous_le_bail_dun_autre_rend_409_row_locked`.
"""
from __future__ import annotations

import uuid

SUB = "sub-230"


def _ligne() -> tuple[int, str]:
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "baux-" + uuid.uuid4().hex[:6])
    db.datastore_insert_row(ns_id, "r1", {"statut": "a_faire"})
    return ns_id, "r1"


def _run() -> str:
    return "run-" + uuid.uuid4().hex[:8]


def _bail(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_run, rev, claims FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone())


def test_renouveler_hors_run_garde_le_run_qui_tient_la_ligne(live):
    from oto_mcp import db
    ns_id, rid = _ligne()
    r1 = _run()
    assert db.datastore_claim_row(ns_id, rid, worker="w", run_id=r1)["claimed_run"] == r1

    renouvele = db.datastore_claim_row(ns_id, rid, worker="w", run_id=None)

    assert renouvele is not None, "le même libellé renouvelle"
    assert renouvele["claimed_run"] == r1
    assert _bail(ns_id, rid)["claimed_run"] == r1
    # Conséquence : la fermeture du run rend toujours la ligne.
    assert db.datastore_release_by_run(r1) == 1


def test_renouveler_sous_un_autre_run_ne_rattache_pas_la_ligne(live):
    """Le libellé n'est pas une identité : il prolonge le bail, il ne le transfère pas."""
    from oto_mcp import db
    ns_id, rid = _ligne()
    r1, r2 = _run(), _run()
    db.datastore_claim_row(ns_id, rid, worker="w", run_id=r1)

    renouvele = db.datastore_claim_row(ns_id, rid, worker="w", run_id=r2)

    assert renouvele["claimed_run"] == r1
    assert db.datastore_release_by_run(r2) == 0
    assert db.datastore_release_by_run(r1) == 1


def test_reprendre_un_bail_echu_pose_le_run_de_l_appel(live):
    """Un bail échu ne protège plus rien : le reprendre est une PRISE, même par le
    même libellé — le run de l'appel devient celui de la ligne, et le compteur de
    reprises monte (#433)."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns_id, rid = _ligne()
    r1, r2 = _run(), _run()
    db.datastore_claim_row(ns_id, rid, worker="w", run_id=r1)
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 second' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, rid))

    repris = db.datastore_claim_row(ns_id, rid, worker="w", run_id=r2)

    assert repris["claimed_run"] == r2
    assert int(repris["claims"]) == 2


def test_une_liberation_puis_la_fermeture_du_run_n_avancent_pas_la_revision(live):
    """Après une libération ligne à ligne, la fermeture du run ne touche plus la
    ligne : la révision lue après la libération reste celle en place."""
    from oto_mcp import db
    ns_id, rid = _ligne()
    r1 = _run()
    db.datastore_claim_row(ns_id, rid, worker="w", run_id=r1)
    assert db.datastore_release_claim(ns_id, rid, "w") is True
    lue = _bail(ns_id, rid)
    assert lue["claimed_run"] is None

    assert db.datastore_release_by_run(r1) == 0
    assert _bail(ns_id, rid)["rev"] == lue["rev"], "avance fantôme de la révision"

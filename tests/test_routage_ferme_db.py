"""Une org ROUTÉE vers la ferme (option d'org `claude_farm`, 29/09/2026) : ses travaux
`anthropic` ne sont réservés QUE par un worker de la ferme (`engine=farm`), jamais par
la boucle, et sans worker de ferme vivant ils attendent — `runner_arme` ne déclare pas
la famille servie pour elle. En SQL, donc sur vraie base.

⚠️ Un claim de PLATEFORME (`org_id=None`) prend le travail de n'importe quelle org :
chaque banc vide la file de ses orgs avant de conclure, et utilise des orgs qui lui sont
propres.
"""
from __future__ import annotations

import pytest


def _router(org, on=True):
    from oto_mcp import db
    if on:
        db.set_option_comp("org", str(org), db.OPTION_FERME, granted_by="banc")
    else:
        db.clear_option_comp("org", str(org), db.OPTION_FERME)


def _enfiler(org, famille=None):
    from oto_mcp import db
    charge = {"input": "go", "tools": []}
    if famille:
        charge["model_family"] = famille
    return db.enqueue_job(org, "start", payload=charge)["id"]


def _vider(*orgs):
    from oto_mcp import db
    for famille, ferme in ((None, False), ("anthropic", True), ("anthropic", False)):
        while db.claim_next_job(None, "w-vidange", lease_seconds=60, depot=famille,
                                org_ids=list(orgs), ferme=ferme):
            pass


def _effacer_presences():
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM runner_platform_depots")


def test_org_routee_le_worker_de_ferme_prend_son_travail(live):
    from oto_mcp import db
    _router(9401)
    job = _enfiler(9401, "anthropic")
    pris = db.claim_next_job(None, "w-ferme", lease_seconds=60, depot="anthropic",
                             org_ids=[9401], ferme=True)
    assert pris and pris["id"] == job
    _vider(9401)
    _router(9401, on=False)


def test_org_routee_la_boucle_ne_voit_pas_son_travail(live):
    from oto_mcp import db
    _router(9402)
    job = _enfiler(9402, "anthropic")
    assert db.claim_next_job(None, "w-boucle", lease_seconds=60, depot="anthropic",
                             org_ids=[9402]) is None, (
        "un worker de la boucle a pris un travail qu'une org a réservé à la ferme")
    # Le travail ATTEND : il n'a ni échoué ni périmé, et la ferme le prend ensuite.
    pris = db.claim_next_job(None, "w-ferme", lease_seconds=60, depot="anthropic",
                             org_ids=[9402], ferme=True)
    assert pris and pris["id"] == job
    _vider(9402)
    _router(9402, on=False)


def test_org_routee_ses_autres_familles_restent_a_la_boucle(live):
    """La ferme ne sert que `anthropic` : router l'org ne prive pas ses agents d'une
    autre famille de leur worker."""
    from oto_mcp import db
    autre = "mistral"
    _router(9403)
    job = _enfiler(9403, autre)
    pris = db.claim_next_job(None, "w-boucle", lease_seconds=60, depot=autre,
                             org_ids=[9403])
    assert pris and pris["id"] == job
    _vider(9403)
    _router(9403, on=False)


def test_org_non_routee_rien_ne_change(live):
    from oto_mcp import db
    job = _enfiler(9404, "anthropic")
    pris = db.claim_next_job(None, "w-boucle", lease_seconds=60, depot="anthropic",
                             org_ids=[9404])
    assert pris and pris["id"] == job
    _vider(9404)


def test_une_option_echue_ne_route_plus(live):
    from oto_mcp import db
    from datetime import datetime, timedelta, timezone
    db.set_option_comp("org", "9405", db.OPTION_FERME, granted_by="banc",
                       expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    job = _enfiler(9405, "anthropic")
    pris = db.claim_next_job(None, "w-boucle", lease_seconds=60, depot="anthropic",
                             org_ids=[9405])
    assert pris and pris["id"] == job
    _vider(9405)
    _router(9405, on=False)


def test_sans_worker_de_ferme_la_famille_n_est_pas_servie_pour_l_org_routee(live):
    """La boucle vivante ne rend pas `anthropic` servi à une org routée : elle poserait
    un agent qui ne tournerait jamais (`model_not_served`, raison nommée)."""
    from oto_mcp import db
    _effacer_presences()
    _router(9406)
    db.claim_next_job(None, "w-boucle-vivante", lease_seconds=60, depot="anthropic")
    etat = db.runner_arme(9406)
    assert etat["farm_routed"] is True
    assert "anthropic" not in etat["families"]
    # Une org non routée voit la boucle servir `anthropic`, comme avant.
    autre = db.runner_arme(9407)
    assert autre["farm_routed"] is False and "anthropic" in autre["families"]
    # Un worker de ferme vivant pour l'org routée rend la famille servie.
    db.claim_next_job(None, "w-ferme-vivante", lease_seconds=60, depot="anthropic",
                      org_ids=[9406], ferme=True)
    assert "anthropic" in db.runner_arme(9406)["families"]
    # … et pas pour une autre org routée qu'il ne nomme pas.
    _router(9408)
    assert "anthropic" not in db.runner_arme(9408)["families"]
    _effacer_presences()
    _router(9406, on=False)
    _router(9408, on=False)


def test_la_presence_de_ferme_se_lit_comme_sa_famille_pour_les_autres(live):
    """Une présence marquée `#ferme` compte comme `anthropic` pour une org non routée
    qu'elle sert : jamais une famille « anthropic#ferme » inventée."""
    from oto_mcp import db
    _effacer_presences()
    db.claim_next_job(None, "w-ferme-seule", lease_seconds=60, depot="anthropic",
                      ferme=True)
    familles = db.runner_arme(9409)["families"]
    assert familles == ["anthropic"]
    _effacer_presences()

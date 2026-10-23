"""`db.agent_du_travail` : l'agent dont un travail est le run — sur une vraie base.

C'est par lui que `oto_fleet op=create` range un passage sous l'agent hébergé qui l'a
déclaré (`runner_fleets.trigger_id`). Il ne résout que l'agent posé par le serveur dans
la charge d'un travail de la même org : ni un travail sans agent, ni un agent d'une autre
org, ni un agent supprimé.
"""
from __future__ import annotations

ORG = 8411


def _agent(db, org=ORG):
    return db.create_trigger(org, "proprio", procedure="veille", tz="UTC", tools=["a"],
                             cron="0 8 * * *")


def _travail(db, org, payload):
    return db.enqueue_job(org, "start", payload=payload, sub="proprio")["id"]


def test_le_run_d_un_agent_resout_cet_agent(live):
    from oto_mcp import db
    a = _agent(db)
    j = _travail(db, ORG, {"procedure": "veille", "trigger_id": a["id"]})
    assert db.agent_du_travail(j, ORG) == a["id"]


def test_un_travail_sans_agent_ne_resout_rien(live):
    from oto_mcp import db
    j = _travail(db, ORG, {"procedure": "veille"})
    assert db.agent_du_travail(j, ORG) is None
    assert db.agent_du_travail(j + 10_000, ORG) is None, "travail introuvable"


def test_un_agent_d_une_autre_org_ne_se_resout_pas(live):
    """Une charge qui nommerait l'agent d'une autre org, ou un travail lu depuis une
    autre org que la sienne : rien."""
    from oto_mcp import db
    ailleurs = _agent(db, ORG + 1)
    j = _travail(db, ORG, {"procedure": "veille", "trigger_id": ailleurs["id"]})
    assert db.agent_du_travail(j, ORG) is None
    a = _agent(db)
    k = _travail(db, ORG, {"procedure": "veille", "trigger_id": a["id"]})
    assert db.agent_du_travail(k, ORG + 1) is None


def test_un_agent_supprime_ne_se_resout_plus(live):
    from oto_mcp import db
    a = _agent(db)
    j = _travail(db, ORG, {"procedure": "veille", "trigger_id": a["id"]})
    assert db.delete_trigger(a["id"], ORG)
    assert db.agent_du_travail(j, ORG) is None

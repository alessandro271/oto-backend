"""Le partage d'un agent par le réglage de son org (23/10/2026) — en base.

1. `visibilites_d_agents` dit, en UNE requête, qui voit chaque agent au-delà de son
   propriétaire : le rôle du partage VIVANT à l'org, et le nombre de personnes nommées.
2. `orgs.default_sharing` se pose et se relit tel quel (la validation est au module de
   capacité, `test_partage_par_defaut.py`).
3. Le partage né du réglage est MARQUÉ : il se retire sans toucher aux partages faits à
   la main, et il ne prête aucun forfait personnel.
Joué sur une vraie base.
"""
from __future__ import annotations

import psycopg

ORG = 8310


def _agent(db, org=ORG, **kw):
    return db.create_trigger(org, "proprio", procedure="veille", tz="UTC", tools=["a"],
                             cron="0 8 * * *", **kw)


def test_qui_voit_chaque_agent(live, pg_module_dsn):
    from oto_mcp import db
    a, b, c, d = _agent(db), _agent(db), _agent(db), _agent(db)
    db.grant_resource("runner_trigger", str(a["id"]), "org", str(ORG), role="editor")
    db.grant_resource("runner_trigger", str(a["id"]), "user", "x", role="viewer")
    db.grant_resource("runner_trigger", str(b["id"]), "org", str(ORG), role="viewer")
    db.grant_resource("runner_trigger", str(c["id"]), "org", str(ORG), role="editor")
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s", (str(c["id"]),))
    assert db.visibilites_d_agents([a["id"], b["id"], c["id"], d["id"]], ORG) == {
        a["id"]: {"org": "editor", "people": 1},
        b["id"]: {"org": "viewer", "people": 0},
        c["id"]: {"org": None, "people": 0},   # échu : ne donne plus rien
        d["id"]: {"org": None, "people": 0},
    }
    # Un partage à une AUTRE org ne rend pas l'agent visible dans la sienne.
    assert db.visibilites_d_agents([b["id"]], ORG + 1)[b["id"]]["org"] is None
    assert db.visibilites_d_agents([], ORG) == {}


def test_le_reglage_se_pose_et_se_relit(live, pg_module_dsn):
    from oto_mcp import db
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        org = c.execute("INSERT INTO orgs (name) VALUES ('Réglage') RETURNING id"
                        ).fetchone()[0]
    assert db.partage_par_defaut_stocke(org) is None, "une org d'avant : rien de posé"
    assert db.poser_partage_par_defaut(org, {"agent": "lecture"})
    assert db.partage_par_defaut_stocke(org) == {"agent": "lecture"}
    assert db.poser_partage_par_defaut(org, None)
    assert db.partage_par_defaut_stocke(org) is None
    assert db.poser_partage_par_defaut(10**12, {"agent": "lecture"}) is False


def test_le_partage_du_reglage_est_marque_et_ne_prete_aucun_forfait(live):
    from oto_mcp import db
    t = _agent(db, model="sub:sonnet")
    rid, tid = str(t["id"]), t["id"]
    db.grant_resource("runner_trigger", rid, "org", str(ORG), role="viewer",
                      granted_by=db.MARQUE_PARTAGE_PAR_DEFAUT)
    db.grant_resource("runner_trigger", rid, "user", "nomme", role="editor",
                      granted_by="proprio")
    db.grant_resource("runner_trigger", rid, "group", "12", role="viewer",
                      granted_by="proprio")
    assert db.forfaits_pretes([tid], "quelqu-un-de-l-org") == set(), (
        "le partage né du réglage ne prête pas le forfait du propriétaire")
    assert db.forfaits_pretes([tid], "nomme") == {tid}, "le prêt nommé reste le seul"

    # Le retirer — par sa marque — laisse les partages faits à la main.
    from oto_mcp.db import _conn as dbconn
    with dbconn._connect() as c:
        retires = c.execute(
            "DELETE FROM resource_grants WHERE resource_type = 'runner_trigger' "
            "AND granted_by = %s AND resource_id = %s",
            (db.MARQUE_PARTAGE_PAR_DEFAUT, rid)).rowcount
    assert retires == 1
    assert db.visibilites_d_agents([tid], ORG)[tid] == {"org": None, "people": 2}

"""Le rang d'affichage d'un lien de projet (`op=rank_link`).

Ranger les tableaux d'un projet n'est pas reposer leurs liens : ni contrôle de portée de
la cible, ni `projects.updated_at`, ni ligne au fil — le rang seul. Le SQL est lu tel
qu'émis, sans base.
"""
import contextlib

import pytest

from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import projects as DBP

CTX = ResolvedCtx(sub="u1", org_id=99)
ROW = {"id": 7, "owner_type": "org", "owner_id": "99", "name": "Proj", "brief_md": "b",
       "created_by": "u1", "archived_at": None, "created_at": "2026-06-30",
       "updated_at": "2026-06-30"}


@pytest.fixture
def banc(monkeypatch):
    rec = {"rank": [], "activity": [], "update": [], "upsert": [], "matches": 1}
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW, id=pid) if pid == 7 else None)
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.db, "rank_project_link",
                        lambda pid, tt, ref, identity_ref=None, position=None: (
                            rec["rank"].append((pid, tt, ref, identity_ref, position)) or rec["matches"]))
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: rec["activity"].append(a[2]))
    monkeypatch.setattr(P.db, "update_project", lambda *a, **k: rec["update"].append(a))
    monkeypatch.setattr(P.db, "add_project_link", lambda *a, **k: rec["upsert"].append(a))
    return rec


def test_rang_pose_seul(banc):
    """Le rang part en base, et rien d'autre : ni fil, ni projet, ni lien reposé."""
    out = P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                         target_ref="912", position=1.5))
    assert banc["rank"] == [(7, "tableau", "912", None, 1.5)]
    assert (banc["activity"], banc["update"], banc["upsert"]) == ([], [], [])
    assert out["position"] == 1.5


def test_rang_retire(banc):
    P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                   target_ref="912", clear_position=True))
    assert banc["rank"] == [(7, "tableau", "912", None, None)]


@pytest.mark.parametrize("champs", [{}, {"position": 2.0, "clear_position": True},
                                    {"position": float("inf")}, {"position": float("nan")}])
def test_rang_mal_dit(banc, champs):
    """Ni rang ni retrait, les deux à la fois, ou un rang non fini : refus nommé, rien d'écrit."""
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                       target_ref="912", **champs))
    assert (e.value.status, e.value.code) == (400, "invalid_position")
    assert banc["rank"] == []


def test_champ_non_lu_refuse(banc):
    """Ranger n'est pas reposer : un libellé passé à rank_link est refusé, pas ignoré."""
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                       target_ref="912", position=1.0, label="Pipeline"))
    assert (e.value.status, e.value.code) == (400, "param_not_read_by_op")
    assert banc["rank"] == []


def test_rang_seul_face_mcp(banc):
    """Sur la face MCP, chaque champ arrive à son défaut : ce n'est pas un champ posé."""
    tous = {n: c.default for n, c in P.ProjectInput.model_fields.items() if n != "op"}
    P._project(CTX, P.ProjectInput(**dict(tous, op="rank_link", project_id=7,
                                          target_type="tableau", target_ref="912", position=3.0)))
    assert banc["rank"] == [(7, "tableau", "912", None, 3.0)]


def test_lien_introuvable(banc):
    banc["matches"] = 0
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                       target_ref="404", position=1.0))
    assert (e.value.status, e.value.code) == (404, "link_not_found")


def test_ecriture_requise(banc, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": want != "write")
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="rank_link", project_id=7, target_type="tableau",
                                       target_ref="912", position=1.0))
    assert e.value.status == 403
    assert banc["rank"] == []


@pytest.fixture
def emis(monkeypatch):
    requetes = []

    class _Res:
        rowcount = 1

    class _Conn:
        def execute(self, sql, params=()):
            requetes.append((" ".join(sql.split()), params))
            return _Res()

    monkeypatch.setattr(DBP, "_connect", lambda: contextlib.nullcontext(_Conn()))
    return requetes


def test_sql_rang_seul(emis):
    """Une seule écriture, sur la colonne du rang : pas de `projects.updated_at`."""
    assert DBP.rank_project_link(7, "tableau", 912, position=2.5) == 1
    assert emis == [("UPDATE project_links SET position = %s WHERE project_id = %s AND "
                     "target_type = %s AND target_ref = %s AND identity_ref IS NOT DISTINCT FROM %s",
                     (2.5, 7, "tableau", "912", None))]

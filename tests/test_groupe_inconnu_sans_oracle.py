"""F-016 — `group.use` / `group.set_home` ne disent pas si l'équipe #N existe.

Un groupe absent rendait 404 `unknown_group`, un groupe d'une autre org 403
`not_a_member` : les ids étant séquentiels, tout compte énumérait les équipes de la
plateforme. Même refus pour les deux, sur les trois faces (MCP, REST, maison), et
avec ou sans le verrou d'un agent hébergé. Sans base : `group_store` est doublé.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import session_org, verrou_org
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.groups import core as gc

MOI = "u-moi"
EXISTE, ABSENT = 3, 999        # 3 : équipe d'une autre org ; 999 : aucun groupe


@pytest.fixture
def monde(monkeypatch):
    poses: list = []
    monkeypatch.setattr(gc.group_store, "get_group",
                        lambda gid: {"id": gid, "name": "Equipe", "org_id": 8}
                        if gid == EXISTE else None)
    monkeypatch.setattr(gc.group_store, "is_group_member", lambda sub, gid: False)
    monkeypatch.setattr(gc.group_store, "set_active_group",
                        lambda sub, gid: poses.append((sub, gid)) and False)
    return poses


def _refus(fn, gid):
    with pytest.raises(AuthzDenied) as e:
        fn(SimpleNamespace(sub=MOI), gc.UseGroupInput(group_id=gid))
    return e.value.status, e.value.code, e.value.message


@pytest.mark.parametrize("face", ["use_mcp", "use_rest", "set_home"])
@pytest.mark.parametrize("sous_verrou", [False, True])
def test_groupe_absent_et_groupe_d_autrui_meme_refus(monkeypatch, monde, face, sous_verrou):
    monkeypatch.setattr(session_org, "current_session_id",
                        lambda: "sid-1" if face == "use_mcp" else None)
    fn = gc._set_home_group if face == "set_home" else gc._use_group
    t = (verrou_org.poser(verrou_org.Verrou(sub=MOI, org_id=7, job_id=1))
         if sous_verrou else None)
    try:
        assert _refus(fn, EXISTE) == _refus(fn, ABSENT)
    finally:
        if t is not None:
            verrou_org.lever(t)
    if not sous_verrou:
        assert _refus(fn, ABSENT)[:2] == (403, "not_a_member")

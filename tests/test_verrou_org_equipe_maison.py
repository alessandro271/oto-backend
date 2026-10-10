"""L'équipe maison ne se change pas sous le jeton d'un agent hébergé (`verrou_org.py`).

`group_store.set_active_group` pose l'équipe maison ET son org parente en maison : la face
REST de `group.use` (`PUT /api/me/active-group`) et `group.set_home`
(`PUT /api/me/home-group`) déplaçaient donc l'org maison de la personne sans la garde
qu'`org.set_home` applique. Même refus nommé ici (`org_out_of_job`), rien n'est écrit ;
la face MCP de `group.use` (un indice sans état) reste servie. Sans base : `group_store`
est doublé.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import session_org, verrou_org
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.groups import core as gc

PORTEUR = "u-porteur"


@pytest.fixture
def poses(monkeypatch):
    poses: list = []
    monkeypatch.setattr(gc.group_store, "get_group",
                        lambda gid: {"id": gid, "name": "Equipe", "org_id": 8})
    monkeypatch.setattr(gc.group_store, "is_group_member", lambda sub, gid: True)
    monkeypatch.setattr(gc.group_store, "set_active_group",
                        lambda sub, gid: poses.append((sub, gid)) or True)
    return poses


@pytest.fixture
def verrou():
    t = verrou_org.poser(verrou_org.Verrou(sub=PORTEUR, org_id=7, job_id=1))
    yield
    verrou_org.lever(t)


def _rest(monkeypatch):
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)


@pytest.mark.parametrize("geste", ["set_home", "use_rest"])
def test_l_equipe_maison_est_refusee_sous_le_jeton(monkeypatch, poses, verrou, geste):
    _rest(monkeypatch)
    ctx = SimpleNamespace(sub=PORTEUR)
    fn = gc._set_home_group if geste == "set_home" else gc._use_group
    with pytest.raises(AuthzDenied) as e:
        fn(ctx, gc.UseGroupInput(group_id=3))
    assert e.value.code == "org_out_of_job"
    assert poses == []


def test_l_indice_mcp_de_group_use_reste_servi_sous_le_jeton(monkeypatch, poses, verrou):
    monkeypatch.setattr(session_org, "current_session_id", lambda: "sid-1")
    out = gc._use_group(SimpleNamespace(sub=PORTEUR), gc.UseGroupInput(group_id=3))
    assert out["session_state"] is None and poses == []


@pytest.mark.parametrize("geste", ["set_home", "use_rest"])
def test_sans_jeton_verrouille_rien_ne_change(monkeypatch, poses, geste):
    _rest(monkeypatch)
    fn = gc._set_home_group if geste == "set_home" else gc._use_group
    fn(SimpleNamespace(sub=PORTEUR), gc.UseGroupInput(group_id=3))
    assert poses == [(PORTEUR, 3)]


def test_un_tiers_n_est_pas_borne_par_le_jeton_du_porteur(monkeypatch, poses, verrou):
    _rest(monkeypatch)
    gc._set_home_group(SimpleNamespace(sub="u-tiers"), gc.UseGroupInput(group_id=3))
    assert poses == [("u-tiers", 3)]

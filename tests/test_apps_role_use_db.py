"""Le rôle Use d'une app (`contributor`), contre un PostgreSQL réel : il se pose sur une
app seulement, se lit `read` au plan contenu, et fait cliquer sans faire modifier. Le
rôle se lit en UNE requête quel que soit le nombre de principals ; l'écran des apps le
distingue d'un `viewer` (`db.apps.visibilites`)."""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    owner, use, view = f"owner_{u}", f"use_{u}", f"view_{u}"
    for s in (owner, use, view):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"a_{u}", created_by=owner)
    org_store.add_org_member(org, owner, "org_admin")
    app = db.apps.create(("user", owner), org, title=f"Desk {u}", manifest={}, html="",
                         thumbnail=None, by=owner)
    return {"owner": owner, "use": use, "view": view, "org": org, "app": app["id"], "u": u}


def _partager(m, rtype, rid, email, role):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    return R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org"]),
                        ResourceInputV2(op="share", resource_type=rtype, resource_id=str(rid),
                                        audience="person", email=email, role=role))


def test_use_se_lit_read_et_clique_sans_modifier(monde):
    from oto_mcp import ownership
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    out = _partager(m, "app", m["app"], f"{m['use']}@example.test", "contributor")
    assert (out["role"], out["permission"]) == ("contributor", "read")
    _partager(m, "app", m["app"], f"{m['view']}@example.test", "viewer")
    rid = str(m["app"])
    assert ownership.can_access(m["use"], "app", rid, "read")
    assert not ownership.can_access(m["use"], "app", rid, "write")
    assert C.peut_cliquer(m["use"], m["app"])
    assert ownership.can_access(m["view"], "app", rid, "read")
    assert not C.peut_cliquer(m["view"], m["app"])
    assert C.peut_cliquer(m["owner"], m["app"])
    roles = {g["principal_id"]: g["role"] for g in ownership.list_grants("app", rid)}
    assert roles == {m["use"]: "contributor", m["view"]: "viewer"}


def test_l_outil_herite_ne_prend_pas_contributor():
    """La surface héritée est sous cliquet : le rôle Use n'entre que par la v2."""
    from pydantic import ValidationError
    from oto_mcp.capabilities.resources import ResourceInput
    with pytest.raises(ValidationError):
        ResourceInput(op="share", resource_type="app", resource_id="1", role="contributor")


def test_le_demarrage_ne_ramene_pas_use_a_view(monde):
    """Le démarrage recalcule le rôle des partages depuis `permission` ; `contributor`
    (projection `read`) n'en est pas un à recalculer — sinon chaque boot le rendrait
    `viewer`."""
    from oto_mcp import db
    from oto_mcp.db import _init
    m = monde
    _partager(m, "app", m["app"], f"{m['use']}@example.test", "contributor")
    with db._connect() as conn:
        _init.apply_boot_schema(conn)
    roles = {g["principal_id"]: g["role"] for g in db.list_resource_grants("app", str(m["app"]))}
    assert roles[m["use"]] == "contributor"


def test_use_n_existe_que_sur_une_app(monde):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    projet = db.create_project("user", m["owner"], f"p_{m['u']}", created_by=m["owner"])
    with pytest.raises(AuthzDenied) as e:
        _partager(m, "project", projet, f"{m['use']}@example.test", "contributor")
    assert e.value.code == "role_for_apps_only"


def test_le_check_de_la_base_prend_contributor_et_rien_d_autre(monde):
    import psycopg
    from oto_mcp import db
    m = monde
    db.grant_resource("app", str(m["app"]), "user", m["view"], role="contributor",
                      granted_by=m["owner"])
    with pytest.raises(psycopg.errors.CheckViolation):
        with db._connect() as conn:
            conn.execute("UPDATE resource_grants SET role = 'superuser' "
                         "WHERE resource_type = 'app' AND resource_id = %s", (str(m["app"]),))

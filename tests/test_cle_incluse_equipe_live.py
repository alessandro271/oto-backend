"""La clé incluse attribuable à une ÉQUIPE (#1239, décision du 13/10/2026), en SQL réel.

Le scope `group` (l'équipe ACTIVE, dont l'appelant est membre dans l'org active) entre
`user` et `org` : dans la chaîne (`grants_chain.grantee_scopes`) et en miroir dans
l'ancien chemin (`access.platform_grant._platform_grantee_scope`). Posé par le super admin
de l'instance (REST et `oto_admin_key_grant scope=group`) — sur une instance dédiée, c'est
l'opérateur de cette instance : le tenant ne s'étend plus (convention du 08/10/2026).

Le cas cible, avec la clé incluse : un connecteur réservé à l'équipe « Ventes » — clé
plateforme accordée à l'équipe, clé perso interdite par l'org. Un membre de Ventes passe,
un autre membre est refusé à l'appel, par le nom de la politique.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp import access, grants_chain
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.mcp_errors import McpError

CHAINE = "fullenrich"       # basculé sur la chaîne, clé payante fermée
ANCIEN = "dropcontact"      # ancien chemin (`share_down`), clé payante fermée


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_incluse_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "8" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url), ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(params=["legacy", "chain"])
def traversee(request, monkeypatch):
    monkeypatch.setenv("OTO_L7_DECIDE", request.param)
    return request.param


@pytest.fixture
def monde(live, traversee, monkeypatch):
    """Org O : un admin ; `vendeur` dans l'équipe Ventes (active) ; `autre`, membre sans
    équipe ; `intrus`, qui se dit actif dans Ventes sans en être membre."""
    from oto_mcp import db, group_store, org_store
    _exec("DELETE FROM grant_counters")
    _exec("DELETE FROM grants")
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")
    u = uuid.uuid4().hex[:8]
    admin, vendeur, autre, intrus = (f"{r}_{u}"
                                     for r in ("admin", "vendeur", "autre", "intrus"))
    for s in (admin, vendeur, autre, intrus):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"Org {u}", created_by=admin)
    org_store.add_org_member(org, admin, "org_admin")
    for s in (vendeur, autre, intrus):
        org_store.add_org_member(org, s)
    ventes = group_store.create_group(org, "Ventes")
    group_store.add_group_member(ventes, vendeur)
    actives = {vendeur: ventes, intrus: ventes}
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "current_group", lambda sub: actives.get(sub))
    monkeypatch.setattr(access, "project_pinned_instance", lambda p: None)
    return {"org": org, "admin": admin, "vendeur": vendeur, "autre": autre,
            "intrus": intrus, "ventes": ventes, "traversee": traversee}


def _resoudre(conn: str, sub: str):
    return access.resolve_credential(conn, sub=sub, emit_on_failure=False)


def _super_admin() -> ResolvedCtx:
    return ResolvedCtx(sub="operateur", org_id=None)


# ── le scope `group` : l'équipe ACTIVE, et seulement la sienne ────────────────

def test_le_scope_equipe_est_l_equipe_active_dont_on_est_membre(monde):
    org, v = str(monde["org"]), str(monde["ventes"])
    assert grants_chain.grantee_scopes(monde["vendeur"], monde["org"]) == [
        ("user", monde["vendeur"]), ("group", v), ("org", org)]
    assert grants_chain.grantee_scopes(monde["intrus"], monde["org"]) == [
        ("user", monde["intrus"]), ("org", org)]           # active, pas membre : non
    assert grants_chain.grantee_scopes(monde["autre"], monde["org"]) == [
        ("user", monde["autre"]), ("org", org)]


# ── côté plateforme : la clé incluse posée sur une équipe ─────────────────────

@pytest.mark.parametrize("conn", [CHAINE, ANCIEN])
def test_la_cle_incluse_accordee_a_l_equipe_ne_sert_que_ses_membres(monde, conn):
    if conn == ANCIEN and monde["traversee"] == "chain":
        pytest.skip("sous la chaîne, une liste `share_down` d'un connecteur non basculé est "
                    "« hors modèle » pour TOUT scope (user et org compris) : rien de neuf")
    from oto_mcp import credentials_store
    from oto_mcp.access.resolve import CredentialUnavailable
    from oto_mcp.capabilities import admin_console
    credentials_store.set_platform_key("incluse", conn, f"{conn}-PLAT", set_by="op")
    out = admin_console._key_grant(_super_admin(), admin_console.KeyGrantInput(
        op="grant", scope="group", group_id=monde["ventes"], provider=conn,
        daily_quota=5))
    assert out == {"ok": True, "group_id": monde["ventes"], "provider": conn,
                   "daily_quota": 5}
    rc = _resoudre(conn, monde["vendeur"])
    assert (rc.key, rc.mode) == (f"{conn}-PLAT", "platform")
    for sub in (monde["autre"], monde["intrus"]):
        with pytest.raises(CredentialUnavailable):
            _resoudre(conn, sub)
    admin_console._key_grant(_super_admin(), admin_console.KeyGrantInput(
        op="revoke", scope="group", group_id=monde["ventes"], provider=conn))
    with pytest.raises(McpError):
        _resoudre(conn, monde["vendeur"])


def test_l_equipe_inconnue_est_refusee_par_son_nom(monde):
    from oto_mcp.capabilities import users_admin
    with pytest.raises(AuthzDenied) as e:
        users_admin._grant_group_key(_super_admin(), users_admin.GroupGrantKeyInput(
            group_id=987654, provider=CHAINE))
    assert (e.value.status, e.value.code) == (404, "unknown_group")


def test_cas_cible_avec_la_cle_incluse(monde):
    """Ventes a la clé incluse, l'org interdit la clé perso : le vendeur passe par la clé
    plateforme, l'autre membre — qui a pourtant SA clé perso — est refusé par la
    politique, nommée."""
    from oto_mcp import credentials_store, db
    from oto_mcp.capabilities import users_admin
    from oto_mcp.connectors import cle_perso
    credentials_store.set_platform_key("incluse", CHAINE, "FE-PLAT", set_by="op")
    users_admin._grant_group_key(_super_admin(), users_admin.GroupGrantKeyInput(
        group_id=monde["ventes"], provider=CHAINE))
    db.set_member_api_key(monde["autre"], monde["org"], CHAINE, "FE-PERSO-AUTRE")
    db.set_member_api_key(monde["vendeur"], monde["org"], CHAINE, "FE-PERSO-VENDEUR")
    cle_perso.poser(monde["org"], CHAINE, True, set_by=monde["admin"])
    rc = _resoudre(CHAINE, monde["vendeur"])
    assert (rc.key, rc.mode) == ("FE-PLAT", "platform")
    with pytest.raises(cle_perso.ClePersoInterdite) as e:
        _resoudre(CHAINE, monde["autre"])
    assert "personal_key_forbidden" in e.value.error.message

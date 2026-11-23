"""Les gestes SENSIBLES refusent un jeton d'API, sur les deux faces.

Deux crans (`capabilities/_session_exigee.py`), testés là où ils s'APPLIQUENT :

1. `RestBinding.allow_api_token=False` : on JOUE la route et on lit ce qui part vers
   `authenticate` (même instrument que `test_api_tokens_capability.py`) — relire le
   descripteur prouverait que le champ est posé, pas qu'il est appliqué. Et un témoin :
   les routes laissées ouvertes à dessein le restent (`GET /api/orgs/{id}`, que lit un
   service tiers ; ajouter ou inviter un simple membre ; le partage) ;
2. la garde de HANDLER : sous un jeton (geste REST `api`, secret de worker, jeton `oto_`
   sur la face MCP), les handlers de domaine refusent en 403 `api_token_forbidden` — par
   les consoles MCP comme par REST — et laissent passer la session (geste `console`,
   session OAuth MCP) et l'appel interne (aucun geste, aucun jeton).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from _datastore_rest import cap

from oto_mcp import geste
from oto_mcp.auth import hooks, platform_worker
from oto_mcp.capabilities import _rest_adapter, _session_exigee
from oto_mcp.capabilities._types import AuthzDenied


# ── 1. Le binding REST ───────────────────────────────────────────────────────

def _kwargs_d_authentification(cle: str, index: int) -> dict:
    vus: dict = {}

    async def _auth(_req, _verifier, **kw):
        vus.update(kw)
        return None, JSONResponse({"error": "stop"}, status_code=401)

    c = cap(cle)
    binding = c.rest_bindings()[index]
    handler = _rest_adapter._make_handler(
        c, binding, None, _auth,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None: JSONResponse({"error": code}, status_code=s))

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(handler(Request({"type": "http", "method": binding.verb,
                                 "path": binding.path, "query_string": b"",
                                 "headers": [], "path_params": {}}, receive=_receive)))
    return vus


_SESSION_SEULE = [
    ("org.archive", 0), ("org.admin.archive", 0),
    ("org.member.set_role", 0), ("org.member.set_role", 1),
    ("org.member.remove", 0), ("org.member.remove", 1),
    ("group.member.set_role", 0), ("group.member.remove", 0),
    ("org.mfa.set", 0), ("platform.user.set_role", 0), ("platform.user.reset_mfa", 0),
    ("billing.subscribe", 0), ("billing.confirm", 0), ("billing.cancel", 0),
    ("billing.resume", 0), ("billing.method_change", 0),
    ("billing.method_change_confirm", 0),
    # Gestes d'administration (#1913) : rôles et droits, état d'un compte ou d'un tenant,
    # clés et secrets, plan et contrat.
    ("admin.tenant_admin_add", 0), ("admin.tenant_admin_remove", 0),
    ("admin.account", 0), ("admin.tenant_disablement", 0),
    ("billing.admin_set_plan", 0), ("billing.admin_set_contract", 0),
    ("billing.admin_cancel_contract", 0),
    ("admin.tenant_key_set", 0), ("admin.tenant_key_clear", 0),
    ("admin.orgs.billing_identity.set", 0),
    ("admin.tenant_app_set", 0), ("admin.tenant_app_clear", 0),
    ("platform.editor_app.set", 0), ("platform.editor_app.delete", 0),
    ("platform.key.create", 0), ("platform.key.delete", 0),
    ("platform.runner.worker", 0), ("platform.option.set", 0),
    # Déjà là avant ce lot — gardés ici pour que la liste dise TOUT ce qui est fermé.
    ("org.api_key.create", 0), ("org.api_key.delete", 0),
    ("platform.token.create", 0), ("platform.token.delete", 0),
]


@pytest.mark.parametrize("cle,index", _SESSION_SEULE)
def test_la_route_sensible_refuse_le_porteur_de_jeton(cle, index):
    assert _kwargs_d_authentification(cle, index).get("allow_api_token") is False, cle
    assert not _kwargs_d_authentification(cle, index).get("allow_issuer_token"), (
        f"{cle} : le jeton émetteur ne franchit que les routes de SES jetons")


@pytest.mark.parametrize("cle,index", [
    ("org.get", 0), ("org.member.add", 0), ("org.invite.create", 0),
    ("group.member.add", 0),
    ("platform.key.grant", 0), ("platform.key.revoke", 0),
    ("connectors.bulk_select", 0),
    # Restent ouverts en attendant une décision (#1913) : leur fermeture tiendrait en
    # une ligne (`allow_api_token=False` sur le binding).
    ("platform.group.grant_key", 0), ("platform.group.revoke_key", 0),
    ("platform.connector.access_set", 0), ("org.admin.create", 0),
    ("org.secret.set", 0), ("org.secret.set", 1),
    ("org.secret.delete", 0), ("org.secret.delete", 1)])
def test_les_routes_laissees_ouvertes_le_restent(cle, index):
    """Lecture générale, ajout/invitation d'un simple membre (la garde de handler juge le
    rang), les routes d'administration qu'un service tiers appelle avec son jeton, et
    celles qui attendent une décision."""
    assert "allow_api_token" not in _kwargs_d_authentification(cle, index), cle


# ── Les routes appelées sous jeton par un service d'instance ─────────────────
#
# Liste NOMINATIVE, par verbe et chemin : un service d'instance les appelle sous un
# jeton `oto_`. Elles restent ouvertes au jeton sur les deux crans — le binding ne le
# refuse pas, et le handler ne pose pas la garde. Une fermeture qui en toucherait une
# casse ici.

_APPELEES_SOUS_JETON = [
    ("GET", "/api/admin/tenants/{slug}"),
    ("GET", "/api/admin/tenants/{slug}/keys"),
    ("GET", "/api/admin/tenants/{slug}/keys/{provider}/grants"),
    ("PUT", "/api/admin/tenants/{slug}/keys/{provider}/grants/{org_id}"),
    ("DELETE", "/api/admin/tenants/{slug}/keys/{provider}/grants/{org_id}"),
    ("GET", "/api/admin/platform-keys"),
    ("GET", "/api/admin/connectors/{provider}/platform-access"),
    ("POST", "/api/admin/orgs/{id}/grants/{provider}"),
    ("DELETE", "/api/admin/orgs/{id}/grants/{provider}"),
    ("GET", "/api/admin/orgs/{id}/unipile-limit"),
    ("PUT", "/api/admin/orgs/{id}/unipile-limit"),
    ("GET", "/api/admin/usage/first-calls"),
    ("POST", "/api/admin/orgs/{id}/suspension"),
    ("POST", "/api/admin/orgs/{id}/trial"),
    ("GET", "/api/admin/orgs/{id}"),
    ("GET", "/api/admin/orgs"),
    ("GET", "/api/admin/unipile/seats"),
    ("DELETE", "/api/admin/unipile/seats/{account_id}"),
]


def _par_chemin(verbe: str, chemin: str):
    from oto_mcp.capabilities import registry
    trouves = [(c, i) for c in registry.CAPABILITIES
               for i, b in enumerate(c.rest_bindings())
               if (b.verb, b.path) == (verbe, chemin)]
    assert len(trouves) == 1, f"{verbe} {chemin} : {len(trouves)} binding(s)"
    return trouves[0]


class _GardeAppelee(Exception):
    pass


@pytest.mark.parametrize("verbe,chemin", _APPELEES_SOUS_JETON,
                         ids=[f"{v} {c}" for v, c in _APPELEES_SOUS_JETON])
def test_appelee_sous_jeton_par_un_service_d_instance(verbe, chemin, sous_jeton_rest,
                                                       monkeypatch):
    """Appelées sous jeton par un service d'instance : jamais 403 `api_token_forbidden`."""
    c, index = _par_chemin(verbe, chemin)
    # Cran 1 : l'authentification de la route accepte le jeton.
    assert "allow_api_token" not in _kwargs_d_authentification(c.key, index), c.key

    # Cran 2 : le handler, joué sous un jeton, n'appelle pas la garde. Elle lit
    # `porteur_de_jeton` à l'appel : la remplacer la fait SIGNALER, avant toute autre
    # lecture ; ce que le handler fait ensuite (une base absente…) n'est pas jugé ici.
    def _signale():
        raise _GardeAppelee(c.key)
    monkeypatch.setattr(_session_exigee, "porteur_de_jeton", _signale)

    def _pas_de_base():
        raise RuntimeError("pas de base dans ce test")
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_get_pool", _pas_de_base)
    try:
        champs = {n: (5 if f.annotation in (int, Optional[int]) else "x")
                  for n, f in c.Input.model_fields.items() if f.is_required()}
        inp = c.Input.model_construct(**champs)
        resultat = c.handler(SimpleNamespace(sub="u-jeton", org_id=5, role=None,
                                             group_id=None, channel="rest"), inp)
        if asyncio.iscoroutine(resultat):
            asyncio.run(resultat)
    except _GardeAppelee:
        pytest.fail(f"{verbe} {chemin} : le handler pose `exiger_session`")
    except Exception as e:  # noqa: BLE001 — tout sauf la garde : hors du sujet
        assert getattr(e, "code", None) != "api_token_forbidden", (verbe, chemin)


# ── 2. La garde de handler ───────────────────────────────────────────────────

@pytest.fixture()
def sous_jeton_rest():
    jeton = geste.poser(geste.API, "u-jeton")
    yield
    geste.retirer(jeton)


@pytest.fixture()
def sous_session_rest():
    jeton = geste.poser(geste.CONSOLE, "u-session")
    yield
    geste.retirer(jeton)


@pytest.fixture()
def sous_jeton_mcp(monkeypatch):
    """Ce que `server._verify_api_token` pose sur une requête MCP portée par `oto_`."""
    monkeypatch.setattr(hooks, "current_token_axes",
                        lambda: {"token_id": 7, "token_kind": "user"})


def test_la_garde_reconnait_chaque_porteur(monkeypatch):
    assert _session_exigee.porteur_de_jeton() is False          # appel interne
    with geste.portee(geste.CONSOLE, "u"):
        assert _session_exigee.porteur_de_jeton() is False      # dashboard (JWT)
    with geste.portee(geste.AGENT, "u"):
        assert _session_exigee.porteur_de_jeton() is False      # MCP sous OAuth
    with geste.portee(geste.API, "u"):
        assert _session_exigee.porteur_de_jeton() is True       # REST sous `oto_`
    platform_worker.set_current({"worker_sub": "w"})
    try:
        assert _session_exigee.porteur_de_jeton() is True       # secret de worker
    finally:
        platform_worker.set_current(None)
    monkeypatch.setattr(hooks, "current_token_axes", lambda: {"token_id": 3})
    with geste.portee(geste.AGENT, "u"):
        assert _session_exigee.porteur_de_jeton() is True       # MCP sous `oto_`


def test_un_jeton_mcp_se_reconnait_aussi_a_son_client(monkeypatch):
    import fastmcp.server.dependencies as dep
    monkeypatch.setattr(hooks, "current_token_axes", lambda: {})
    monkeypatch.setattr(dep, "get_access_token",
                        lambda: SimpleNamespace(client_id="oto_api_token", claims={}))
    assert _session_exigee.porteur_de_jeton() is True
    monkeypatch.setattr(dep, "get_access_token",
                        lambda: SimpleNamespace(client_id="claude.ai", claims={}))
    assert _session_exigee.porteur_de_jeton() is False


def _refus(fn, *a) -> tuple[int, str]:
    with pytest.raises(AuthzDenied) as e:
        fn(*a)
    return e.value.status, e.value.code


_REFUS = (403, "api_token_forbidden")
_CTX = SimpleNamespace(sub="u-admin", org_id=5, role="org_admin", group_id=None,
                       channel=None)


def _gestes():
    """Chaque geste sensible, appelé par SON handler de domaine — celui que servent la
    route REST ET la console MCP. La garde tombe avant toute lecture en base."""
    from oto_mcp.capabilities import (account_suspension as susp, admin_console, billing,
                                      org_console, resources, tenant_admins as ta,
                                      tenant_desactivation as td,
                                      tenant_keys as tk, tenants_admin, users_admin)
    from oto_mcp.capabilities.groups import members as gm
    from oto_mcp.capabilities.orgs import admin as oa, members as om, mfa, update
    return {
        "org.archive": lambda: update._archive_org(_CTX, update.OrgIdInput(org_id=5)),
        "oto_org op=archive": lambda: org_console._org(
            _CTX, org_console.OrgInput(op="archive", org_id=5)),
        "org.admin.archive": lambda: oa._archive_org(_CTX, oa.OrgIdInput(org_id=5)),
        "oto_admin_org op=archive": lambda: admin_console._org(
            _CTX, admin_console.OrgAdminInput(op="archive", org_id=5)),
        "org.member.remove": lambda: om._remove_member(
            _CTX, om.RemoveMemberInput(org_id=5, target="u-x")),
        "oto_admin_org_member op=remove": lambda: admin_console._org_member(
            _CTX, admin_console.OrgMemberAdminInput(op="remove", org_id=5, target="u-x")),
        "group.member.remove": lambda: gm._remove_member(
            _CTX, gm.RemoveGroupMemberInput(group_id=9, target="u-x")),
        "org.mfa.set": lambda: mfa._set_org_mfa(
            _CTX, mfa.SetOrgMfaInput(org_id=5, require=False)),
        "platform.user.set_role": lambda: users_admin._set_role(
            _CTX, users_admin.SetRoleInput(target="u-x", role="super_admin")),
        "platform.user.reset_mfa": lambda: users_admin._reset_mfa(
            _CTX, users_admin.ResetMfaInput(target="u-x")),
        "oto_resource op=transfer": lambda: resources._resources(
            _CTX, resources.ResourceInput(op="transfer", resource_type="project",
                                          resource_id="12", new_owner_org=5)),
        # Gestes d'administration (#1913). La console MCP appelle le même handler de
        # domaine que la route REST : chacun est joué par les deux entrées.
        "admin.tenant_admin_add": lambda: ta._add(
            _CTX, ta.TenantAdminAddInput(slug="pilote", sub="u-x")),
        "oto_admin_tenant op=admin_add": lambda: tenants_admin._console(
            _CTX, _console(op="admin_add", slug="pilote", sub="u-x")),
        "admin.tenant_admin_remove": lambda: ta._remove(
            _CTX, ta.TenantAdminRemoveInput(slug="pilote", sub="u-x")),
        "oto_admin_tenant op=admin_remove": lambda: tenants_admin._console(
            _CTX, _console(op="admin_remove", slug="pilote", sub="u-x")),
        "admin.account op=suspend": lambda: susp._account(
            _CTX, susp.AccountSuspensionInput(op="suspend", target="u-x", reason="r")),
        "admin.account op=resume": lambda: susp._account(
            _CTX, susp.AccountSuspensionInput(op="resume", target="u-x")),
        "admin.tenant_disablement op=disable": lambda: td._disablement(
            _CTX, td.TenantDisablementInput(slug="pilote", op="disable", reason="r")),
        "oto_admin_tenant op=disable": lambda: tenants_admin._console(
            _CTX, _console(op="disable", slug="pilote", reason="r")),
        "oto_admin_tenant op=enable": lambda: tenants_admin._console(
            _CTX, _console(op="enable", slug="pilote")),
        "admin.tenant_key_clear": lambda: tk._clear_key(
            _CTX, tk.TenantKeyClearInput(slug="pilote", provider="serper")),
        "oto_admin_tenant op=key_clear": lambda: tenants_admin._console(
            _CTX, _console(op="key_clear", slug="pilote", provider="serper")),
        "billing.admin_set_plan": lambda: billing._admin_set_plan(
            _CTX, billing.AdminPlanInput(org_id=5, plan="pro")),
        "billing.admin_set_contract": lambda: billing._admin_set_contract(
            _CTX, billing.AdminContractInput(org_id=5, plan="pro", seats=3)),
        "billing.admin_cancel_contract": lambda: billing._admin_cancel_contract(
            _CTX, billing.AdminContractCancelInput(org_id=5)),
        "platform.option.set": lambda: users_admin._set_option(
            _CTX, users_admin.OptionInput(entity_type="user", entity_id="u-x",
                                          option="beta", on=True)),
    }


def _console(**champs):
    from oto_mcp.capabilities import tenants_admin
    return tenants_admin.TenantConsoleInput(**champs)


@pytest.fixture()
def rien_en_base(monkeypatch):
    """Toute lecture en base lève : la garde doit refuser AVANT d'en faire une."""
    from oto_mcp import db, org_store

    def _boum(*a, **k):
        raise AssertionError("lecture en base avant la garde")
    for nom in ("get_org", "get_org_role", "list_org_members", "remove_org_member"):
        monkeypatch.setattr(org_store, nom, _boum)
    monkeypatch.setattr(db, "get_user", _boum)


@pytest.mark.parametrize("nom", list(_gestes()))
def test_le_geste_sensible_refuse_un_jeton_rest(nom, sous_jeton_rest, rien_en_base):
    assert _refus(_gestes()[nom]) == _REFUS, nom


@pytest.mark.parametrize("nom", list(_gestes()))
def test_le_geste_sensible_refuse_un_jeton_mcp(nom, sous_jeton_mcp, rien_en_base):
    with geste.portee(geste.AGENT, "u-jeton"):
        assert _refus(_gestes()[nom]) == _REFUS, nom


def test_la_session_passe_la_garde(sous_session_rest, monkeypatch):
    """La garde ne refuse que le jeton : sous une session, le handler va plus loin — ici
    jusqu'à sa propre lecture de l'org, doublée pour répondre « inconnue »."""
    from oto_mcp import org_store
    from oto_mcp.capabilities.orgs import update
    monkeypatch.setattr(org_store, "get_org", lambda oid: None)
    assert _refus(update._archive_org, _CTX, update.OrgIdInput(org_id=5)) == (
        404, "unknown_org")


def test_sous_session_le_geste_d_administration_va_plus_loin(sous_session_rest):
    """Sous une session, la garde laisse la main au handler : ici le refus qu'il porte
    lui-même (le plan d'une org se décide dans le service de facturation)."""
    from oto_mcp.capabilities import billing
    status, code = _refus(billing._admin_set_plan, _CTX,
                          billing.AdminPlanInput(org_id=5, plan="pro"))
    assert (status, code) == (409, "billing_moved")


# ── Les gestes conditionnels ─────────────────────────────────────────────────

@pytest.fixture()
def membres(monkeypatch):
    """`_write_member_role` sans base : le rôle courant est lu d'un dict, l'écriture
    est enregistrée."""
    from oto_mcp import org_store
    from oto_mcp.capabilities.orgs import members as om
    roles = {"u-membre": "org_member", "u-admin2": "org_admin"}
    ecrits: list = []
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: roles.get(sub))
    monkeypatch.setattr(org_store, "add_org_member",
                        lambda org, sub, role, actor=None: ecrits.append((sub, role)))
    monkeypatch.setattr(om, "_count_org_admins", lambda org: 2)
    return ecrits


def test_sous_jeton_on_ajoute_un_simple_membre(sous_jeton_rest, membres):
    from oto_mcp.capabilities.orgs import members as om
    om._write_member_role(5, "u-neuf", "org_member", require_member=False, actor="u")
    om._write_member_role(5, "u-membre", "org_member", require_member=False, actor="u")
    assert membres == [("u-neuf", "org_member"), ("u-membre", "org_member")]


@pytest.mark.parametrize("sub,role", [
    ("u-neuf", "org_admin"),        # nommer un admin, même à l'ajout
    ("u-membre", "org_admin"),      # promouvoir
    ("u-admin2", "org_member")])    # rétrograder par `add` (l'upsert, #273)
def test_sous_jeton_ni_admin_ni_changement_de_role(sous_jeton_rest, membres, sub, role):
    from oto_mcp.capabilities.orgs import members as om
    with pytest.raises(AuthzDenied) as e:
        om._write_member_role(5, sub, role, require_member=False, actor="u")
    assert (e.value.status, e.value.code) == _REFUS
    assert membres == []


def test_sous_session_le_role_change(sous_session_rest, membres):
    from oto_mcp.capabilities.orgs import members as om
    om._write_member_role(5, "u-membre", "org_admin", require_member=True, actor="u")
    assert membres == [("u-membre", "org_admin")]


@pytest.mark.parametrize("role,group_role,refuse", [
    ("org_admin", None, True), ("org_member", "group_admin", True),
    ("org_member", None, False), ("org_member", "group_member", False)])
def test_l_invitation_n_est_sensible_que_si_elle_donne_le_rang_d_admin(
        sous_jeton_rest, monkeypatch, role, group_role, refuse):
    """Les trois émetteurs (org, équipe, plateforme) passent par `emit_invitation`."""
    from oto_mcp.capabilities.orgs import invites as oi
    monkeypatch.setattr(oi.org_store, "org_front", lambda org: (None, None))
    monkeypatch.setattr(oi.org_store, "create_invitation", lambda *a, **k: (1, "inv_T"))
    monkeypatch.setattr(oi.config, "invite_base_url", lambda: "https://front.test")
    appel = lambda: oi.emit_invitation(  # noqa: E731
        _CTX, org_id=5, email=None, send_email=False, source="org_admin", role=role,
        target_name="acme", group_id=9 if group_role else None, group_role=group_role)
    if refuse:
        assert _refus(appel) == _REFUS
    else:
        assert appel()["invite_url"] == "https://front.test/invitation/inv_T"


# ── 3. Bout en bout REST : la VRAIE authentification jusqu'au geste conditionnel ──
#
# Les tests ci-dessus posent le geste à la main ; celui-ci passe par
# `api.base._authenticate` (un jeton `oto_` tel que la base le rend, `token_kind`
# compris ou ABSENT) puis l'adaptateur, jusqu'à une invitation d'admin — un geste sans
# cran de binding, que seule la garde de handler tient.

def _jouer_avec_la_vraie_auth(monkeypatch, ligne: dict, role: str) -> tuple[int, dict]:
    import json

    from oto_mcp.api import routes as api_routes
    from oto_mcp.auth import token_scopes
    from oto_mcp.capabilities import _authz
    from oto_mcp.capabilities.orgs import invites as oi
    monkeypatch.setattr(api_routes.db, "verify_api_token", lambda t: dict(ligne))
    monkeypatch.setattr(api_routes.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(api_routes.db, "get_suspension", lambda sub: None)
    from oto_mcp import garde_identite
    monkeypatch.setattr(garde_identite, "refus", lambda sub: None)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(oi.org_store, "get_org", lambda oid: {"id": oid, "name": "acme"})
    monkeypatch.setattr(oi.org_store, "org_front", lambda org: (None, None))
    monkeypatch.setattr(oi.org_store, "create_invitation", lambda *a, **k: (1, "inv_T"))
    monkeypatch.setattr(oi.config, "invite_base_url", lambda: "https://front.test")

    c = cap("org.invite.create")
    binding = c.rest_bindings()[0]
    handler = _rest_adapter._make_handler(
        c, binding, None, api_routes._authenticate,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None, details=None: JSONResponse({"error": code},
                                                                status_code=s))
    corps = json.dumps({"role": role, "send_email": False}).encode()

    async def _receive():
        return {"type": "http.request", "body": corps, "more_body": False}

    async def _jouer():
        try:
            rep = await handler(Request({
                "type": "http", "method": "POST", "path": "/api/orgs/5/invitations",
                "query_string": b"", "path_params": {"id": "5"},
                "headers": [(b"authorization", b"Bearer oto_deadbeef"),
                            (b"content-type", b"application/json")]}, _receive))
        finally:
            token_scopes.set_current(None)
        return rep.status_code, json.loads(bytes(rep.body))
    return asyncio.run(_jouer())


@pytest.mark.parametrize("ligne", [
    {"sub": "u-jeton", "scopes": None, "token_id": 7, "token_kind": "user"},
    {"sub": "u-jeton", "scopes": None, "token_id": 7}],     # `token_kind` absent
    ids=["kind-user", "kind-absent"])
def test_bout_en_bout_un_jeton_n_invite_pas_un_admin(monkeypatch, ligne):
    code, corps = _jouer_avec_la_vraie_auth(monkeypatch, ligne, "org_admin")
    assert (code, corps.get("error")) == _REFUS, corps


def test_bout_en_bout_un_jeton_invite_un_simple_membre(monkeypatch):
    code, corps = _jouer_avec_la_vraie_auth(
        monkeypatch, {"sub": "u-jeton", "scopes": None, "token_id": 7,
                      "token_kind": "user"}, "org_member")
    assert code == 200 and corps["invite_url"] == "https://front.test/invitation/inv_T", corps

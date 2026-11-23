"""Des routes d'administration ouvertes à l'identité de service `commerce` (`SERVICE_OU`).

Chaque route est jouée de bout en bout par le vrai adaptateur REST et la vraie
authentification (`api.base._authenticate`), sous trois porteurs :
- un client machine de rôle `commerce` (vrai JWT ES384, vrai verifier des services) :
  accepté, le handler reçoit le contexte du service ;
- un client machine sans ce rôle : refusé ;
- un jeton `oto_` d'admin plateforme : accepté, comme avant.

Le handler de chaque capacité est remplacé par un témoin : ce banc juge QUI passe, pas
ce que fait la route (ses propres bancs le font). La règle `SERVICE_OU` est aussi jugée
seule : sans identité de service posée, la règle de compte d'origine décide seule.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest
from authlib.jose import JsonWebKey, jwt as authlib_jwt
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp import access
from oto_mcp.api import base as ab
from oto_mcp.auth import platform_worker, service_identity, token_scopes
from oto_mcp.capabilities import _authz, _rest_adapter, registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

_LOGTO = "https://annuaire.exemple.test"
_ISSUER = f"{_LOGTO}/oidc"
_AUD_SERVICES = "https://mcp.exemple.test/api/service"   # conftest : adresse publique
_CLIENT = "m2m-facturation-1"
_ADMIN = "u-admin-plateforme"

# Clé → (plancher plateforme de la règle de compte, chemin concret, requête, corps).
_ROUTES = {
    "admin.tenant": ("operator", "/api/admin/tenants/pilote", "", None),
    "admin.tenant_keys": ("operator", "/api/admin/tenants/pilote/keys", "", None),
    "admin.tenant_org_grants": (
        "operator", "/api/admin/tenants/pilote/keys/serper/grants", "", None),
    "admin.tenant_org_grant": (
        "super", "/api/admin/tenants/pilote/keys/serper/grants/7", "", {"daily_quota": 5}),
    "admin.tenant_org_revoke": (
        "super", "/api/admin/tenants/pilote/keys/serper/grants/7", "", None),
    "platform.key.list": ("super", "/api/admin/platform-keys", "", None),
    "platform.connector.access_list": (
        "operator", "/api/admin/connectors/serper/platform-access", "", None),
    "platform.org.grant_key": ("super", "/api/admin/orgs/7/grants/serper", "", {}),
    "platform.org.revoke_key": ("super", "/api/admin/orgs/7/grants/serper", "", None),
    "platform.org.unipile_limit_get": ("operator", "/api/admin/orgs/7/unipile-limit", "", None),
    "platform.org.unipile_limit_set": (
        "super", "/api/admin/orgs/7/unipile-limit", "", {"limit": 3}),
    "platform.usage.first_calls": (
        "operator", "/api/admin/usage/first-calls", "org_ids=7", None),
    "org.admin.list": ("operator", "/api/admin/orgs", "", None),
    "org.admin.get": ("operator", "/api/admin/orgs/7", "", None),
    "admin.unipile_seats": ("super", "/api/admin/unipile/seats", "", None),
    "admin.unipile_seat_release": ("super", "/api/admin/unipile/seats/acc-1", "", None),
}
# Les planchers d'origine : `TENANT_ADMIN_OF` n'a pas de plancher (sa cible décide).
_PLANCHERS = {"admin.tenant": None, "admin.tenant_keys": None,
              "admin.tenant_org_grants": None, "admin.tenant_org_grant": None,
              "admin.tenant_org_revoke": None}


def _cap(cle):
    return next(c for c in registry.CAPABILITIES if c.key == cle)


def _binding(cap):
    (b,) = cap.rest_bindings()
    return b


# ── l'annuaire et les porteurs ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def cle():
    return JsonWebKey.generate_key("EC", "P-384", is_private=True)


@pytest.fixture(autouse=True)
def annuaire(monkeypatch, cle):
    """L'émetteur primaire et son JWKS, servis sans réseau."""
    monkeypatch.setenv("LOGTO_ENDPOINT", _LOGTO)
    service_identity._verifier_pour.cache_clear()
    pem = JsonWebKey.import_key(cle.as_dict(is_private=False)).as_pem().decode()

    async def cle_servie(self, token):
        return pem
    from fastmcp.server.auth.providers.jwt import JWTVerifier
    monkeypatch.setattr(JWTVerifier, "_get_verification_key", cle_servie)
    yield
    service_identity.set_current(None)
    platform_worker.set_current(None)
    token_scopes.set_current(None)
    service_identity._verifier_pour.cache_clear()


@pytest.fixture
def admin_plateforme(monkeypatch):
    """Un jeton `oto_` d'un admin plateforme (super admin) — sans base."""
    monkeypatch.setattr(ab.db, "verify_api_token",
                        lambda token: {"sub": _ADMIN, "scopes": None, "token_id": 1,
                                       "token_kind": None} if token == "oto_ADMIN" else None)
    monkeypatch.setattr(ab.garde_identite, "refus", lambda sub: None)
    monkeypatch.setattr(ab, "_maybe_view_as", lambda sub, apply=True: sub)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: sub == _ADMIN)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: sub == _ADMIN)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "super_admin")


def _jeton(cle, *, scope="commerce") -> str:
    now = int(time.time())
    claims = {"iss": _ISSUER, "aud": _AUD_SERVICES, "sub": _CLIENT, "client_id": _CLIENT,
              "iat": now, "exp": now + 3600, "scope": scope}
    return authlib_jwt.encode({"alg": "ES384", "kid": "k1"}, claims, cle).decode()


def _jouer(cle_cap: str, porteur: str) -> tuple[int, dict, list]:
    """Joue la route par le vrai adaptateur et la vraie authentification ; le handler
    est un témoin qui rend le contexte résolu."""
    vus: list = []

    def temoin(ctx: ResolvedCtx, inp):
        vus.append(ctx)
        return {"sub": ctx.sub, "role": ctx.role}

    cap = dataclasses.replace(_cap(cle_cap), handler=temoin, Output=None)
    binding = _binding(cap)
    _, chemin, requete, corps = _ROUTES[cle_cap]
    handler = _rest_adapter._make_handler(
        cap, binding, None, ab._authenticate,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None, **kw: JSONResponse({"error": code}, status_code=s))

    brut = json.dumps(corps).encode() if corps is not None else b""

    async def _receive():
        return {"type": "http.request", "body": brut, "more_body": False}

    # Les paramètres de chemin, lus du chemin concret contre le gabarit.
    params = {g.strip("{}"): v for g, v in zip(binding.path.split("/"), chemin.split("/"))
              if g.startswith("{")}
    entetes = [(b"authorization", f"Bearer {porteur}".encode())]
    if corps is not None:
        entetes.append((b"content-type", b"application/json"))
    req = Request({"type": "http", "method": binding.verb, "path": chemin,
                   "query_string": requete.encode(), "headers": entetes,
                   "path_params": params, "root_path": "", "scheme": "http",
                   "server": ("test", 80), "http_version": "1.1"}, receive=_receive)
    rep = asyncio.run(handler(req))
    return rep.status_code, json.loads(bytes(rep.body)), vus


# ── la déclaration ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("cle_cap", sorted(_ROUTES))
def test_la_route_admin_est_ouverte_au_service_sans_changer_de_chemin(cle_cap):
    cap = _cap(cle_cap)
    assert cap.mcp is None
    assert _binding(cap).path.startswith("/api/admin/")
    assert _authz.accepts_service(cap.authz)
    assert getattr(cap.authz, "service_role", None) == service_identity.COMMERCE


@pytest.mark.parametrize("cle_cap", sorted(_ROUTES))
def test_le_plancher_de_visibilite_reste_celui_de_la_regle_de_compte(cle_cap):
    attendu = _PLANCHERS.get(cle_cap, _ROUTES[cle_cap][0])
    assert _authz.platform_floor(_cap(cle_cap).authz) == attendu


# ── les trois porteurs, de bout en bout ──────────────────────────────────────

@pytest.mark.parametrize("cle_cap", sorted(_ROUTES))
def test_le_service_commerce_est_accepte(cle_cap, cle):
    statut, corps, vus = _jouer(cle_cap, _jeton(cle))
    assert statut == _binding(_cap(cle_cap)).status, corps
    assert corps == {"sub": f"service:{_CLIENT}", "role": "service:commerce"}
    assert vus[0].org_id is None


@pytest.mark.parametrize("cle_cap", sorted(_ROUTES))
def test_un_service_sans_le_role_est_refuse(cle_cap, cle):
    statut, corps, vus = _jouer(cle_cap, _jeton(cle, scope="lecture autre"))
    assert (statut, corps["error"]) == (403, "service_role_missing")
    assert vus == []


@pytest.mark.parametrize("cle_cap", sorted(_ROUTES))
def test_le_jeton_oto_d_un_admin_passe_comme_avant(cle_cap, admin_plateforme):
    statut, corps, vus = _jouer(cle_cap, "oto_ADMIN")
    assert statut == _binding(_cap(cle_cap)).status, corps
    assert corps == {"sub": _ADMIN, "role": "super_admin"}


# ── la règle seule ───────────────────────────────────────────────────────────

_SERVICE = {"sub": f"service:{_CLIENT}", "client_id": _CLIENT,
            "roles": frozenset({"commerce"})}


def test_un_service_d_un_autre_role_est_refuse_par_la_regle():
    regle = _authz.SERVICE_OU(service_identity.COMMERCE, _authz.SUPER_ADMIN)
    service_identity.set_current({**_SERVICE, "roles": frozenset({"autre"})})
    with pytest.raises(AuthzDenied) as refus:
        regle(RawCtx(sub=_SERVICE["sub"]))
    assert (refus.value.status, refus.value.code) == (403, "service_role_missing")


def test_sans_service_pose_la_regle_de_compte_decide_seule(monkeypatch):
    regle = _authz.SERVICE_OU(service_identity.COMMERCE, _authz.SUPER_ADMIN)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    with pytest.raises(AuthzDenied) as refus:
        regle(RawCtx(sub="u-membre"))
    assert (refus.value.status, refus.value.code) == (403, "forbidden")
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    monkeypatch.setattr(access, "current_org", lambda sub: 3)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "super_admin")
    assert regle(RawCtx(sub="u-super")) == ResolvedCtx(sub="u-super", org_id=3,
                                                        role="super_admin")


def test_un_role_hors_catalogue_est_refuse_a_la_declaration():
    with pytest.raises(ValueError):
        _authz.SERVICE_OU("inconnu", _authz.SUPER_ADMIN)


def test_la_fiche_d_org_sert_au_service_le_drapeau_d_operateur(monkeypatch):
    """Même forme de réponse que pour l'opérateur : `is_platform_operator` est servi."""
    from oto_mcp.capabilities.orgs import reads
    vus = {}
    monkeypatch.setattr(reads.org_store, "get_org", lambda oid: {"id": oid, "name": "Acme"})
    monkeypatch.setattr(reads.org_store, "get_org_role", lambda oid, sub: None)
    monkeypatch.setattr(reads.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(reads, "_members",
                        lambda oid, exposer_operateur: vus.setdefault("e", exposer_operateur)
                        and [])
    monkeypatch.setattr(reads.org_store, "effective_logo_url", lambda o: None)
    monkeypatch.setattr(reads.org_store, "is_personal_org", lambda oid: False)
    monkeypatch.setattr(reads.org_store, "get_trial_ends_at", lambda oid: None)
    monkeypatch.setattr(reads.org_store, "list_org_secrets", lambda oid: [])
    monkeypatch.setattr(reads.db, "list_option_comps", lambda et, eid: [])
    ctx = ResolvedCtx(sub=_SERVICE["sub"], org_id=None, role="service:commerce")
    reads._fiche(ctx, reads.OrgIdInput(org_id=7))
    assert vus["e"] is True

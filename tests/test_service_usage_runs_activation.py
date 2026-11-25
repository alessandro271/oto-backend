"""L'usage, les runs, l'ajout au kit et l'activation d'un connecteur, ouverts à
l'identité de service `commerce` par des routes-sœurs sous `/api/service/`.

Cinq gestes, chacun servi aux comptes sous `/api/orgs/{id}/…` et, désormais, au service
sous `/api/service/orgs/{id}/…`, par le MÊME handler. Chaque geste est joué de bout en
bout par le vrai adaptateur REST et la vraie authentification (`api.base._authenticate`) :

- (a) le client machine de rôle `commerce` (vrai JWT ES384) passe sur la route service ;
- (b) un client machine sans ce rôle y est refusé ;
- (c) le jeton `oto_` d'un administrateur de l'org passe sur la route d'org comme avant,
  et n'entre pas par la route service ;
- (d) le jeton `oto_` d'un administrateur d'une AUTRE org reste refusé sur la route d'org.

Le handler est remplacé par un témoin pour juger QUI passe. Ce que fait la route service
est jugé à part : elle vérifie l'org (404 `unknown_org`), puis délègue au handler du
compte, sans seconde implémentation.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import time

import pytest
from authlib.jose import JsonWebKey, jwt as authlib_jwt
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp import access
from oto_mcp.api import base as ab
from oto_mcp.auth import platform_worker, service_identity, token_scopes
from oto_mcp.capabilities import _authz, _org_du_service, _rest_adapter, registry
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

_LOGTO = "https://annuaire.exemple.test"
_ISSUER = f"{_LOGTO}/oidc"
_AUD_SERVICES = "https://mcp.exemple.test/api/service"   # conftest : adresse publique
_CLIENT = "m2m-facturation-1"
_ADMIN_7 = "u-admin-org-7"
_ADMIN_8 = "u-admin-org-8"

# Geste → (capacité du compte, chemin du compte, capacité du service, chemin du service,
#          requête, corps, module et nom du handler du compte).
_GESTES = {
    "usage.tools": (
        "org.usage.tools", "/api/orgs/7/usage/tools",
        "service.org.usage.tools", "/api/service/orgs/7/usage/tools",
        "", None, ("org_monitoring", "_billable_tools")),
    "usage.connections": (
        "org.usage.connections", "/api/orgs/7/usage/connections",
        "service.org.usage.connections", "/api/service/orgs/7/usage/connections",
        "", None, ("org_monitoring", "_connections")),
    "monitoring.runs": (
        "org.monitoring.runs", "/api/orgs/7/monitoring/runs",
        "service.org.monitoring.runs", "/api/service/orgs/7/monitoring/runs",
        "", None, ("org_monitoring", "_runs")),
    "bulk_select": (
        "connectors.bulk_select", "/api/orgs/7/connectors/serper/bulk-select",
        "service.org.connectors.bulk_select",
        "/api/service/orgs/7/connectors/serper/bulk-select",
        "", {}, ("connectors.kit", "_bulk_select")),
    "activation": (
        "connectors.activation.set_org", "/api/orgs/7/connectors/serper/activation",
        "service.org.connectors.activation.set",
        "/api/service/orgs/7/connectors/serper/activation",
        "", {"enabled": True}, ("connectors.activation", "_org_set")),
}


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
def comptes(monkeypatch):
    """Deux jetons `oto_` : l'administrateur de l'org 7, celui de l'org 8 — sans base,
    sans rôle plateforme (aucune escalade)."""
    jetons = {"oto_ADMIN7": _ADMIN_7, "oto_ADMIN8": _ADMIN_8}
    monkeypatch.setattr(ab.db, "verify_api_token",
                        lambda token: {"sub": jetons[token], "scopes": None, "token_id": 1,
                                       "token_kind": None} if token in jetons else None)
    monkeypatch.setattr(ab.garde_identite, "refus", lambda sub, emis_le=None: None)
    monkeypatch.setattr(ab, "_maybe_view_as", lambda sub, apply=True: sub)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    org_de = {_ADMIN_7: 7, _ADMIN_8: 8}
    monkeypatch.setattr(_authz.roles, "is_org_member",
                        lambda sub, org_id: org_de.get(sub) == org_id)
    monkeypatch.setattr(_authz.roles, "is_org_admin",
                        lambda sub, org_id: org_de.get(sub) == org_id)
    monkeypatch.setattr(_authz.detenteurs, "admins_de_l_org", lambda sub, org_id: None)


def _jeton(cle, *, scope="commerce") -> str:
    now = int(time.time())
    claims = {"iss": _ISSUER, "aud": _AUD_SERVICES, "sub": _CLIENT, "client_id": _CLIENT,
              "iat": now, "exp": now + 3600, "scope": scope}
    return authlib_jwt.encode({"alg": "ES384", "kid": "k1"}, claims, cle).decode()


def _jouer(cle_cap: str, chemin: str, requete: str, corps, porteur: str):
    """Joue la route par le vrai adaptateur et la vraie authentification ; le handler
    est un témoin qui rend le contexte résolu."""
    vus: list = []

    def temoin(ctx: ResolvedCtx, inp):
        vus.append((ctx, inp))
        return {"sub": ctx.sub, "role": ctx.role}

    cap = dataclasses.replace(_cap(cle_cap), handler=temoin, Output=None)
    binding = _binding(cap)
    handler = _rest_adapter._make_handler(
        cap, binding, None, ab._authenticate,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None, **kw: JSONResponse({"error": code}, status_code=s))

    brut = json.dumps(corps).encode() if corps is not None else b""

    async def _receive():
        return {"type": "http.request", "body": brut, "more_body": False}

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


def _service(geste):
    _, _, cle_cap, chemin, requete, corps, _ = _GESTES[geste]
    return cle_cap, chemin, requete, corps


def _compte(geste):
    cle_cap, chemin, _, _, requete, corps, _ = _GESTES[geste]
    return cle_cap, chemin, requete, corps


# ── la déclaration ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_la_route_service_est_une_soeur_reservee_au_commerce(geste):
    cap = _cap(_service(geste)[0])
    assert cap.mcp is None
    assert _binding(cap).path.startswith("/api/service/orgs/{id}/")
    assert cap.authz is _authz.COMMERCE_SERVICE


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_la_route_du_compte_garde_sa_regle(geste):
    """Rien n'est ouvert au service sur la route d'org : sa règle ne le lit pas."""
    cap = _cap(_compte(geste)[0])
    assert _binding(cap).path.startswith("/api/orgs/{id}/")
    assert not _authz.accepts_service(cap.authz)


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_meme_entree_meme_sortie_meme_verbe(geste):
    compte, service = _cap(_compte(geste)[0]), _cap(_service(geste)[0])
    assert service.Input is compte.Input
    assert service.Output is compte.Output
    assert _binding(service).verb == _binding(compte).verb
    assert _binding(service).path == _binding(compte).path.replace(
        "/api/orgs/", "/api/service/orgs/", 1)


# ── (a) (b) le service ────────────────────────────────────────────────────────

@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_a_le_service_commerce_est_accepte(geste, cle):
    statut, corps, vus = _jouer(*_service(geste), _jeton(cle))
    assert statut == _binding(_cap(_service(geste)[0])).status, corps
    assert corps == {"sub": f"service:{_CLIENT}", "role": "service:commerce"}
    ctx, inp = vus[0]
    assert ctx.org_id is None
    assert inp.org_id == 7


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_b_un_service_sans_le_role_est_refuse(geste, cle):
    statut, corps, vus = _jouer(*_service(geste), _jeton(cle, scope="lecture autre"))
    assert (statut, corps["error"]) == (403, "service_role_missing")
    assert vus == []


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_b_le_service_n_entre_pas_par_la_route_du_compte(geste, cle):
    """La route d'org n'accepte pas l'identité de service : l'authentification la
    refuse avant toute règle."""
    statut, _, vus = _jouer(*_compte(geste), _jeton(cle))
    assert statut in (401, 403)
    assert vus == []


# ── (c) (d) le jeton `oto_` ──────────────────────────────────────────────────

@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_c_le_jeton_oto_de_l_admin_de_l_org_passe_comme_avant(geste, comptes):
    statut, corps, vus = _jouer(*_compte(geste), "oto_ADMIN7")
    assert statut == _binding(_cap(_compte(geste)[0])).status, corps
    assert corps == {"sub": _ADMIN_7, "role": "member"}
    assert vus[0][0].org_id == 7


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_c_le_jeton_oto_n_entre_pas_par_la_route_service(geste, comptes):
    statut, corps, vus = _jouer(*_service(geste), "oto_ADMIN7")
    assert (statut, corps["error"]) == (403, "service_required")
    assert vus == []


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_d_le_jeton_oto_d_une_autre_org_reste_refuse(geste, comptes):
    statut, corps, vus = _jouer(*_compte(geste), "oto_ADMIN8")
    assert (statut, corps["error"]) == (403, "forbidden")
    assert vus == []


# ── ce que fait la route service : l'org, puis le handler du compte ──────────

_CTX_SERVICE = ResolvedCtx(sub=f"service:{_CLIENT}", org_id=None, role="service:commerce")


def _entree(geste):
    cap = _cap(_service(geste)[0])
    champs = {"org_id": 7}
    if "name" in cap.Input.model_fields:
        champs["name"] = "serper"
    if "enabled" in cap.Input.model_fields:
        champs["enabled"] = True
    return cap, cap.Input(**champs)


@pytest.fixture
def orgs(monkeypatch):
    """L'org 7 existe, la 9 est archivée, les autres n'existent pas."""
    monkeypatch.setattr(_org_du_service.org_store, "get_org",
                        lambda oid: {"id": oid} if oid in (7, 9) else None)
    monkeypatch.setattr(_org_du_service.org_store, "is_archived_org", lambda oid: oid == 9)


def _delegue(monkeypatch, geste):
    """Remplace le handler du compte par un témoin ; rend la liste de ses appels."""
    module, nom = _GESTES[geste][6]
    mod = importlib.import_module(f"oto_mcp.capabilities.{module}")
    appels: list = []
    monkeypatch.setattr(mod, nom, lambda ctx, inp: appels.append((ctx, inp)) or {"ok": 1})
    return appels


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_la_route_service_delegue_au_handler_du_compte(geste, monkeypatch, orgs):
    appels = _delegue(monkeypatch, geste)
    cap, inp = _entree(geste)
    assert cap.handler(_CTX_SERVICE, inp) == {"ok": 1}
    assert appels == [(_CTX_SERVICE, inp)]


@pytest.mark.parametrize("org_id", [8, 9], ids=["inconnue", "archivee"])
@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_une_org_inconnue_ou_archivee_est_refusee_sans_rien_lire_ni_ecrire(
        geste, org_id, monkeypatch, orgs):
    appels = _delegue(monkeypatch, geste)
    cap, inp = _entree(geste)
    with pytest.raises(AuthzDenied) as refus:
        cap.handler(_CTX_SERVICE, inp.model_copy(update={"org_id": org_id}))
    assert (refus.value.status, refus.value.code) == (404, "unknown_org")
    assert appels == []
    assert any((e.status, e.code) == (404, "unknown_org") for e in cap.errors)

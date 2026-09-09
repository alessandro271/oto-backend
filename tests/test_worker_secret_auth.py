"""Un worker de plateforme s'authentifie par un SECRET DE MACHINE, pas par un compte.

Ce que ce fichier ferme, mesuré le 09/09/2026 : les trois agents du runner
tournaient sous le jeton personnel d'un compte admin de quatorze organisations.
Chaque conception qui a précédé faisait PORTER au worker une identité (un compte
marqué, puis un compte qui nomme son org, puis un compte qui lit ses orgs). Ici
le worker n'a qu'un secret ; ce qu'il est, c'est l'authentification REST qui le
POSE après l'avoir vérifié en base, et la règle d'autorisation qui le LIT.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from oto_mcp import access, server
from oto_mcp.api import routes as api_routes
from oto_mcp.auth import platform_worker, token_scopes
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities._types import AuthzDenied, RawCtx

_ROW = {"worker_sub": "worker:ab12cd34", "label": "worker 1"}


def _req(token: str, path: str = "/api/me/runner/jobs"):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "POST", "path": path, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "http_version": "1.1",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


class _Verifier:
    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": "u-jwt"})


def _auth(request, **kw):
    return asyncio.run(api_routes._authenticate(request, verifier=_Verifier(), **kw))


@pytest.fixture
def base(monkeypatch):
    """Le lookup en base, doublé ; le reste de la chaîne d'un jeton `oto_` aussi,
    pour le banc qui vérifie que la variable de contexte ne SURVIT pas."""
    vu = {"secret": None, "api": 0}

    def verify_worker_secret(secret):
        vu["secret"] = secret
        return dict(_ROW) if secret == "otow_bon" else None

    def verify_api_token(t):
        vu["api"] += 1
        return {"sub": "u-1", "scopes": None}

    monkeypatch.setattr(api_routes.db, "verify_worker_secret", verify_worker_secret)
    monkeypatch.setattr(api_routes.db, "verify_api_token", verify_api_token)
    monkeypatch.setattr(api_routes.db, "get_suspension", lambda sub: None)
    yield vu
    platform_worker.set_current(None)
    token_scopes.set_current(None)


# ── La face REST ─────────────────────────────────────────────────────────────

def test_un_secret_valide_authentifie_le_worker_et_le_POSE(base):
    async def scenario():
        sub, err = await api_routes._authenticate(_req("otow_bon"), verifier=_Verifier())
        return sub, err, platform_worker.current()
    sub, err, pose = asyncio.run(scenario())
    assert err is None and sub == "worker:ab12cd34"
    assert pose == _ROW, "la règle d'autorisation lira CE fait, pas la forme du sub"
    assert base["api"] == 0, "un `otow_` ne passe JAMAIS par la table des jetons de compte"


def test_un_secret_inconnu_ou_revoque_est_refuse(base):
    sub, err = _auth(_req("otow_faux"))
    assert sub is None and err.status_code == 401
    assert json.loads(bytes(err.body))["error"] == "invalid_worker_secret"


def test_le_fait_worker_ne_SURVIT_pas_a_la_requete_suivante(base):
    """Une requête d'un compte ordinaire après celle d'un worker : la variable
    est remise à None AVANT de trancher. Sinon un membre hériterait du
    privilège du worker précédent dans la même tâche."""
    async def scenario():
        await api_routes._authenticate(_req("otow_bon"), verifier=_Verifier())
        assert platform_worker.current() == _ROW
        sub, err = await api_routes._authenticate(_req("oto_ordinaire"), verifier=_Verifier())
        return sub, err, platform_worker.current()
    sub, err, pose = asyncio.run(scenario())
    assert err is None and sub == "u-1"
    assert pose is None


def test_un_worker_ne_gere_pas_les_jetons(base):
    sub, err = _auth(_req("otow_bon", "/api/me/tokens"), allow_api_token=False)
    assert sub is None and err.status_code == 403


def test_la_face_MCP_ignore_un_secret_de_worker(monkeypatch):
    """Le worker parle au backend en REST, et aux outils avec le jeton DÉLÉGUÉ.
    Son secret n'a rien à faire sur la face agent : fail-closed, sans même
    consulter la base."""
    def jamais(*a, **k):
        raise AssertionError("la base ne doit pas être consultée pour un otow_")
    monkeypatch.setattr(server.db, "verify_api_token", jamais)
    out = asyncio.run(server._IatGatedVerifier._verify_api_token(object(), "otow_bon"))
    assert out is None


# ── La règle d'autorisation ──────────────────────────────────────────────────

@pytest.fixture
def membre(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    yield
    platform_worker.set_current(None)


def test_worker_pose_donne_un_contexte_SANS_org(membre):
    platform_worker.set_current(dict(_ROW))
    ctx = _authz.WORKER_OR_ORG_MEMBER(RawCtx(sub="worker:ab12cd34"))
    assert ctx.platform_worker is True
    assert ctx.org_id is None, "aucune org : c'est un fait, pas un manque"
    assert ctx.sub == "worker:ab12cd34"


def test_sans_worker_pose_la_regle_EST_org_member(membre):
    ctx = _authz.WORKER_OR_ORG_MEMBER(RawCtx(sub="u1"))
    assert ctx.platform_worker is False and ctx.org_id == 42


def test_un_worker_pose_sous_un_autre_sub_est_refuse(membre):
    """Deux vérités pour une requête : on ne choisit pas."""
    platform_worker.set_current(dict(_ROW))
    with pytest.raises(AuthzDenied) as e:
        _authz.WORKER_OR_ORG_MEMBER(RawCtx(sub="u1"))
    assert e.value.code == "worker_identity_mismatch"

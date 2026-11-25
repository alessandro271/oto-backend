"""Un jeton émis avant la fermeture des sessions de son compte est refusé à l'usage —
`session_revoquee.py`, par la garde d'identité.

La fermeture (`fermer_tout`) pose un instant « révoqué avant » ; la garde le lit DANS la
lecture qu'elle faisait déjà (`db.get_suspension`) et le compare à la date d'émission du
jeton : `iat` d'un JWT, création d'un jeton `oto_`. On joue les vraies portes — REST
(`api.base._authenticate`, JWT puis jeton `oto_`) et MCP (`AccountSuspendedMiddleware`) —,
base simulée. Contrefactuel à chaque fois : le jeton émis APRÈS passe. Puis le coût : une
seule lecture par requête.
"""
from __future__ import annotations

import asyncio
import json
import types
from datetime import datetime, timezone

import pytest

from oto_mcp import db, garde_identite, session_revoquee
from oto_mcp.api import routes as api_routes
from oto_mcp.auth import token_scopes

SUB = "u-ferme"
#: L'instant tel que la lecture de la garde le rend : des secondes, à la microseconde.
FERMETURE = datetime(2026, 11, 25, 10, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
AVANT = FERMETURE - 600                     # dix minutes avant la fermeture
APRES = FERMETURE + 5                       # la reconnexion


@pytest.fixture
def base(monkeypatch):
    """Compte vivant, fermé à `FERMETURE` ; chaque lecture de l'état d'entrée comptée."""
    lectures = []

    def _entree(sub):
        lectures.append(sub)
        return {"sub": sub, "suspended_at": None, "suspended_by": None,
                "suspended_reason": None, "revoque_avant": FERMETURE}
    monkeypatch.setattr(db, "get_suspension", _entree)
    monkeypatch.setattr(db, "upsert_user", lambda *a, **k: None)
    yield lectures
    token_scopes.set_current(None)


def _req(token: str):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "GET", "path": "/api/me", "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80), "http_version": "1.1",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


class _Verifier:
    def __init__(self, claims):
        self.claims = claims

    async def verify_token(self, token):
        return types.SimpleNamespace(claims=dict(self.claims))


def _rest_jwt(claims):
    return asyncio.run(api_routes._authenticate(_req("eyJ.a.b"),
                                                verifier=_Verifier(claims)))


def _rest_oto(monkeypatch, cree_le):
    monkeypatch.setattr(api_routes.db, "verify_api_token",
                        lambda t: {"sub": SUB, "scopes": None, "token_kind": "user",
                                   "token_id": 7, "cree_le": cree_le})
    return asyncio.run(api_routes._authenticate(_req("oto_x"),
                                                verifier=_Verifier({})))


def _refus_nomme(resultat):
    sub, err = resultat
    assert sub is None and err is not None
    assert err.status_code == 401
    assert json.loads(bytes(err.body))["error"] == session_revoquee.CODE


# ── REST, JWT ────────────────────────────────────────────────────────────────

def test_jwt_emis_avant_la_fermeture_est_refuse_401_nomme(base):
    _refus_nomme(_rest_jwt({"sub": SUB, "iat": int(AVANT)}))


def test_jwt_emis_apres_la_fermeture_passe(base):
    assert _rest_jwt({"sub": SUB, "iat": int(APRES)}) == (SUB, None)


def test_jwt_emis_dans_la_seconde_de_la_fermeture_est_refuse(base):
    """L'`iat` est en secondes entières : la seconde même de la fermeture est avant elle."""
    _refus_nomme(_rest_jwt({"sub": SUB, "iat": int(FERMETURE)}))


def test_jwt_sans_iat_est_refuse_des_qu_une_fermeture_a_eu_lieu(base):
    _refus_nomme(_rest_jwt({"sub": SUB}))


def test_compte_jamais_ferme_sert_tout_jwt(base, monkeypatch):
    monkeypatch.setattr(db, "get_suspension", lambda sub: None)
    assert _rest_jwt({"sub": SUB, "iat": 1}) == (SUB, None)


def test_jwt_emis_apres_coute_une_seule_lecture(base):
    """Le sub du jeton est jugé avant le drain d'alias, et l'état lu est repris par la
    garde : une lecture par requête, comme avant."""
    assert _rest_jwt({"sub": SUB, "iat": int(APRES)}) == (SUB, None)
    assert base == [SUB]


# ── REST, JWT d'un ancien identifiant redirigé (drain d'alias) ───────────────

ANCIEN = "u-ancien"


@pytest.fixture
def redirige(monkeypatch):
    """`ANCIEN` (l'identifiant Logto du jeton) est redirigé vers `SUB` par le drain. La
    fermeture est posée sous `ANCIEN` seulement, comme `fermer_tout` la pose (sous l'id
    du webhook) ; le compte canonique, lui, n'a aucun instant."""
    from oto_mcp.api import base as api_base
    lectures = []

    def _entree(sub):
        lectures.append(sub)
        if sub != ANCIEN:
            return None
        return {"sub": sub, "suspended_at": None, "suspended_by": None,
                "suspended_reason": None, "revoque_avant": FERMETURE}
    monkeypatch.setattr(db, "get_suspension", _entree)
    monkeypatch.setattr(db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(api_base, "alias_drain_armed", lambda: True)
    monkeypatch.setattr(api_routes.db, "resolve_sub",
                        lambda s: SUB if s == ANCIEN else s)
    yield lectures
    token_scopes.set_current(None)


def test_jwt_d_un_ancien_identifiant_redirige_emis_avant_est_refuse(redirige):
    """Discriminant sur l'ORDRE : jugé après le drain, sur `SUB` qui n'a pas d'instant,
    ce jeton passerait jusqu'à son terme."""
    _refus_nomme(_rest_jwt({"sub": ANCIEN, "iat": int(AVANT)}))
    assert redirige == [ANCIEN]


def test_jwt_d_un_ancien_identifiant_redirige_emis_apres_passe(redirige):
    """Contrefactuel : la reconnexion passe, servie sous le sub canonique ; la garde lit
    l'état du canonique en plus (une lecture de plus, pour ce seul cas)."""
    assert _rest_jwt({"sub": ANCIEN, "iat": int(APRES)}) == (SUB, None)
    assert redirige == [ANCIEN, SUB]


# ── REST, jeton `oto_` ───────────────────────────────────────────────────────

def test_jeton_oto_cree_avant_la_fermeture_est_refuse_401_nomme(base, monkeypatch):
    """La course de `revoquer_jetons` : un jeton créé avant la fermeture, que la liste
    n'a pas vu, est refusé à l'usage."""
    _refus_nomme(_rest_oto(monkeypatch, AVANT))


def test_jeton_oto_cree_apres_la_fermeture_passe(base, monkeypatch):
    assert _rest_oto(monkeypatch, APRES) == (SUB, None)


# ── MCP ──────────────────────────────────────────────────────────────────────

def _mcp(monkeypatch, client_id, claims):
    from fastmcp.server.auth.auth import AccessToken
    from oto_mcp.middleware import account_suspended as mod
    jeton = AccessToken(token="t", client_id=client_id, scopes=[], subject=SUB,
                        claims={"sub": SUB, **claims})
    monkeypatch.setattr(mod, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: jeton)
    passe = {"n": 0}

    async def _next(ctx):
        passe["n"] += 1
        return "servi"
    return mod.AccountSuspendedMiddleware(), _next, passe


@pytest.mark.parametrize("client_id,claims", [
    ("claude.ai", {"iat": int(AVANT)}),
    ("oto_api_token", {"cree_le": AVANT}),
])
def test_mcp_jeton_emis_avant_est_refuse_nomme(base, monkeypatch, client_id, claims):
    from oto_mcp.mcp_errors import McpError
    mw, _next, passe = _mcp(monkeypatch, client_id, claims)
    with pytest.raises(McpError) as leve:
        asyncio.run(mw.on_request(object(), _next))
    assert leve.value.error.data["code"] == session_revoquee.CODE
    assert passe["n"] == 0


@pytest.mark.parametrize("client_id,claims", [
    ("claude.ai", {"iat": int(APRES)}),
    ("oto_api_token", {"cree_le": APRES}),
])
def test_mcp_jeton_emis_apres_passe(base, monkeypatch, client_id, claims):
    mw, _next, passe = _mcp(monkeypatch, client_id, claims)
    assert asyncio.run(mw.on_request(object(), _next)) == "servi"


# ── La garde : une lecture, un ordre ─────────────────────────────────────────

def test_la_garde_ne_lit_l_etat_d_entree_qu_une_fois(base):
    """Pause et révocation sortent de LA lecture que la garde faisait déjà."""
    assert garde_identite.refus(SUB, AVANT)[0] == session_revoquee.CODE
    assert base == [SUB]


def test_la_pause_passe_avant_la_revocation(monkeypatch):
    monkeypatch.setattr(db, "get_suspension", lambda sub: {
        "sub": sub, "suspended_at": "x", "suspended_reason": "", "revoque_avant": FERMETURE})
    assert garde_identite.refus(SUB, AVANT)[0] == "account_suspended"


def test_une_porte_sans_jeton_date_ne_juge_pas_la_revocation(base):
    """Lien d'upload, travail de fond : pas de jeton daté, pas de refus de ce chef."""
    assert garde_identite.refus(SUB) is None


def test_un_compte_revoque_n_est_pas_en_pause(base):
    from oto_mcp import account_suspension
    assert account_suspension.etat(SUB) is None


def test_statuts_rest_des_refus():
    assert garde_identite.statut(session_revoquee.CODE) == 401
    assert garde_identite.statut("account_suspended") == 403
    assert garde_identite.statut("tenant_disabled") == 403

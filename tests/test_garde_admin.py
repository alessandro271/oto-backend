"""La garde des administrateurs de la plateforme (CASA AL1, 3.3.1 : « le MFA est imposé
à tout compte d'administration ») — `oto_mcp/garde_admin.py`.

Ce que ce banc fige :

1. le rôle d'un opérateur se BORNE au seul point d'où dérivent tous les prédicats
   plateforme (`access.scope.role_effectif`) : jeton `oto_`, session sans second
   facteur fort, facteurs illisibles ⟹ `member` ; session avec TOTP/WebAuthn ⟹ intact ;
2. le porteur seul est concerné — un tiers, un worker, l'absence de requête : rien ;
3. le jeton de délégation ne garde le rôle QUE pendant un `oto_admin_*` que son agent
   nomme, et seulement si son porteur a un facteur enrôlé ;
4. les règles à plancher plateforme DISENT pourquoi elles refusent (code nommé) ;
5. les deux portes posent le porteur POSITIVEMENT (REST : chaque branche
   d'`_authenticate`, y compris le rejeu ; MCP : le jeton vérifié par FastMCP) ;
6. le mode (`OTO_ADMIN_MFA`) : `enforce` par défaut ET sur valeur inconnue ;
7. un tripwire : toute règle à plancher plateforme refuse un opérateur sous jeton d'API.
"""
from __future__ import annotations

import asyncio
import types
import typing

import pytest

from oto_mcp import access, db, garde_admin, verrou_org
from oto_mcp.capabilities import _authz, registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx

OP = "op-1"          # un opérateur (son rôle est doublé par `roles`)
TIERS = "u-2"


@pytest.fixture
def roles(monkeypatch):
    """Le rôle en base, doublé : `db.get_user` est ce que lit `get_user_role`."""
    table = {OP: "super_admin", TIERS: "admin"}
    monkeypatch.setattr(db, "get_user",
                        lambda sub: {"sub": sub, "role": table[sub]} if sub in table else None)
    monkeypatch.delenv("OTO_MCP_ADMIN_SUB", raising=False)
    monkeypatch.delenv("OTO_ADMIN_MFA", raising=False)
    # Les règles enrichissent le ctx de l'org active : sans base ici.
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    return table


@pytest.fixture
def facteurs(monkeypatch):
    """Les facteurs Logto, doublés et COMPTÉS (le défaut du conftest est remplacé)."""
    etat = {"types": ["Totp"], "appels": 0, "leve": None}

    def lire(sub_logto):
        etat["appels"] += 1
        if etat["leve"]:
            raise etat["leve"]
        return list(etat["types"])
    monkeypatch.setattr(garde_admin, "_facteurs_logto", lire)
    garde_admin.oublier()
    return etat


@pytest.fixture
def porteur():
    """Pose le porteur REST de la requête simulée ; le lève au retour."""
    jetons = []

    def poser(nature, sub=OP, sub_logto=None):
        p = garde_admin.Porteur(nature, sub,
                                sub_logto=sub_logto or (sub if nature == garde_admin.SESSION
                                                        else None))
        jetons.append(garde_admin.poser(p))
        return p
    yield poser
    garde_admin.poser(None)


# ── 1. Le rôle borné au seam ─────────────────────────────────────────────────

def test_session_avec_totp_garde_le_role(roles, facteurs, porteur):
    porteur(garde_admin.SESSION)
    assert access.get_user_role(OP) == "super_admin"
    assert access.is_super_admin(OP) and access.is_platform_operator(OP)


@pytest.mark.parametrize("types_", [["WebAuthn"], ["Totp", "BackupCode"]])
def test_les_facteurs_forts_comptent(roles, facteurs, porteur, types_):
    facteurs["types"] = types_
    porteur(garde_admin.SESSION)
    assert access.is_super_admin(OP)


@pytest.mark.parametrize("types_", [[], ["BackupCode"]])
def test_session_sans_facteur_fort_lit_member(roles, facteurs, porteur, types_):
    facteurs["types"] = types_
    porteur(garde_admin.SESSION)
    assert access.get_user_role(OP) == "member"
    assert not access.is_platform_operator(OP)
    assert garde_admin.motif_brut(OP).code == garde_admin.CODE_MFA


def test_logto_injoignable_ferme_et_ne_se_martele_pas(roles, facteurs, porteur):
    facteurs["leve"] = RuntimeError("logto down")
    porteur(garde_admin.SESSION)
    assert access.get_user_role(OP) == "member"
    assert garde_admin.motif_brut(OP).code == garde_admin.CODE_INVERIFIABLE
    access.get_user_role(OP)
    assert facteurs["appels"] == 1, "une panne se cache (TTL court), elle ne se rejoue pas"


def test_le_verdict_se_cache_et_s_oublie(roles, facteurs, porteur):
    porteur(garde_admin.SESSION)
    for _ in range(5):
        assert access.is_super_admin(OP)
    assert facteurs["appels"] == 1
    facteurs["types"] = []
    garde_admin.oublier(OP)
    assert not access.is_super_admin(OP)


def test_facteurs_lus_sur_l_identite_logto_pas_sur_le_canonique(roles, facteurs, porteur,
                                                               monkeypatch):
    vus = []
    monkeypatch.setattr(garde_admin, "_facteurs_logto",
                        lambda s: vus.append(s) or ["Totp"])
    porteur(garde_admin.SESSION, sub=OP, sub_logto="ancien-id-logto")
    assert access.is_super_admin(OP)
    assert vus == ["ancien-id-logto"]


@pytest.mark.parametrize("nature", [garde_admin.JETON, "inconnue"])
def test_jeton_d_api_lit_member_sans_appeler_logto(roles, facteurs, porteur, nature):
    porteur(nature)
    assert access.get_user_role(OP) == "member"
    assert garde_admin.motif_brut(OP).code == garde_admin.CODE_SESSION
    assert facteurs["appels"] == 0


def test_le_bootstrap_n_echappe_pas_a_la_garde(roles, facteurs, porteur, monkeypatch):
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", "boot")
    porteur(garde_admin.JETON, sub="boot")
    assert access.get_user_role("boot") == "member"
    garde_admin.poser(None)
    assert access.get_user_role("boot") == "super_admin"


# ── 2. Le porteur seul ───────────────────────────────────────────────────────

def test_un_tiers_garde_son_role(roles, facteurs, porteur):
    porteur(garde_admin.JETON)
    assert access.get_user_role(TIERS) == "admin"
    assert facteurs["appels"] == 0


def test_hors_requete_rien_n_est_borne(roles, facteurs):
    garde_admin.poser(None)
    assert access.is_super_admin(OP)
    assert facteurs["appels"] == 0


def test_une_machine_n_a_rien_a_borner(roles, facteurs, porteur):
    porteur(garde_admin.MACHINE)
    assert access.is_super_admin(OP)


def test_un_membre_ne_coute_aucun_appel(roles, facteurs, porteur):
    roles[OP] = "member"
    porteur(garde_admin.SESSION)
    assert access.get_user_role(OP) == "member"
    assert facteurs["appels"] == 0


# ── 3. Le jeton de délégation ────────────────────────────────────────────────

def test_delegation_hors_outil_permis_lit_member(roles, facteurs, porteur, monkeypatch):
    monkeypatch.setattr(verrou_org, "outil_admin_permis", lambda sub: False)
    porteur(garde_admin.DELEGATION)
    assert access.get_user_role(OP) == "member"
    assert garde_admin.motif_brut(OP).code == garde_admin.CODE_SESSION


def test_delegation_sur_outil_permis_exige_le_facteur_du_porteur(roles, facteurs, porteur,
                                                                 monkeypatch):
    monkeypatch.setattr(verrou_org, "outil_admin_permis", lambda sub: True)
    porteur(garde_admin.DELEGATION)
    assert access.is_super_admin(OP)
    facteurs["types"] = []
    garde_admin.oublier()
    assert not access.is_super_admin(OP)


def test_outil_admin_permis_lit_le_verrou_et_l_outil(monkeypatch):
    v = verrou_org.Verrou(sub=OP, org_id=1, job_id=7)
    monkeypatch.setattr(verrou_org, "outils_permis", lambda v=None: frozenset(
        {"oto_admin_platform_balances"}))
    jv = verrou_org.poser(v)
    try:
        for outil, attendu in [("oto_admin_platform_balances", True),
                               ("oto_admin_user", False), ("oto_doc", False), (None, False)]:
            jo = verrou_org.poser_outil(outil) if outil else None
            try:
                assert verrou_org.outil_admin_permis(OP) is attendu, outil
                assert verrou_org.outil_admin_permis(TIERS) is False
            finally:
                if jo is not None:
                    verrou_org.lever_outil(jo)
    finally:
        verrou_org.lever(jv)


# ── 4. Les règles DISENT pourquoi ────────────────────────────────────────────

def test_platform_admin_nomme_le_facteur_manquant(roles, facteurs, porteur):
    facteurs["types"] = []
    porteur(garde_admin.SESSION)
    with pytest.raises(AuthzDenied) as e:
        _authz.PLATFORM_ADMIN(RawCtx(sub=OP))
    assert e.value.status == 403 and e.value.code == garde_admin.CODE_MFA


def test_super_admin_nomme_le_jeton(roles, facteurs, porteur):
    porteur(garde_admin.JETON)
    with pytest.raises(AuthzDenied) as e:
        _authz.SUPER_ADMIN(RawCtx(sub=OP))
    assert e.value.code == garde_admin.CODE_SESSION


def test_un_admin_non_super_garde_le_refus_de_la_regle(roles, facteurs, porteur):
    """Même garde levée, un `admin` n'ouvre pas `SUPER_ADMIN` : le refus reste le sien."""
    porteur(garde_admin.JETON, sub=TIERS)
    with pytest.raises(AuthzDenied) as e:
        _authz.SUPER_ADMIN(RawCtx(sub=TIERS))
    assert e.value.code == "forbidden"


def test_un_membre_garde_le_refus_de_la_regle(roles, facteurs, porteur):
    roles[OP] = "member"
    porteur(garde_admin.SESSION)
    with pytest.raises(AuthzDenied) as e:
        _authz.PLATFORM_ADMIN(RawCtx(sub=OP))
    assert e.value.code == "forbidden"


def test_l_escalade_super_admin_vers_une_org_tierce_est_bornee(roles, facteurs, porteur,
                                                              monkeypatch):
    from oto_mcp import org_store, roles as roles_mod
    monkeypatch.setattr(org_store, "get_org_role", lambda org_id, sub: None)
    porteur(garde_admin.SESSION)
    assert roles_mod.is_org_admin(OP, 42)
    garde_admin.poser(garde_admin.Porteur(garde_admin.JETON, OP))
    assert not roles_mod.is_org_admin(OP, 42)


# ── 5. Les portes posent le porteur ──────────────────────────────────────────

def _req(token: str, path: str = "/api/admin/users", method: str = "GET"):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "http_version": "1.1",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


class _Verifier:
    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": "id-logto"})


@pytest.fixture
def porte_rest(monkeypatch):
    from oto_mcp.api import base
    monkeypatch.setattr(base.db, "verify_api_token",
                        lambda t: {"sub": "u-1", "scopes": None, "token_id": 3,
                                   "token_kind": "delegation" if "deleg" in t else "user"})
    monkeypatch.setattr(base.garde_identite, "refus", lambda sub: None)
    monkeypatch.setattr(base.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(base.service_identity, "adresse_aux_services", lambda t: False)
    monkeypatch.setattr(base, "alias_drain_armed", lambda: False)
    yield base
    garde_admin.poser(None)


@pytest.mark.parametrize("bearer,nature,sub,sub_logto", [
    ("jwt.xyz", garde_admin.SESSION, "id-logto", "id-logto"),
    ("oto_ordinaire", garde_admin.JETON, "u-1", None),
    ("oto_deleg", garde_admin.DELEGATION, "u-1", None),
])
def test_rest_pose_le_porteur_positivement(porte_rest, bearer, nature, sub, sub_logto):
    async def scenario():
        # Un jeton de délégation n'entre en REST que par ses routes (le fil du run).
        req = (_req(bearer, "/api/me/runs/thread", "POST")
               if nature == garde_admin.DELEGATION else _req(bearer))
        s, err = await porte_rest._authenticate(req, verifier=_Verifier())
        premier = garde_admin.courant()
        garde_admin.poser(None)
        # Le REJEU (même requête, mêmes options) repose le même porteur.
        await porte_rest._authenticate(req, verifier=_Verifier())
        return err, premier, garde_admin.courant()
    err, premier, rejoue = asyncio.run(scenario())
    assert err is None
    assert premier == garde_admin.Porteur(nature, sub, sub_logto=sub_logto)
    assert rejoue == premier


def test_rest_un_bearer_refuse_ne_laisse_aucun_porteur(porte_rest, monkeypatch):
    monkeypatch.setattr(porte_rest.db, "verify_api_token", lambda t: None)

    async def scenario():
        garde_admin.poser(garde_admin.Porteur(garde_admin.SESSION, OP, sub_logto=OP))
        _, err = await porte_rest._authenticate(_req("oto_faux"), verifier=_Verifier())
        return err, garde_admin._PORTEUR_REST.get()
    err, reste = asyncio.run(scenario())
    assert err.status_code == 401 and reste is None


def _jeton_mcp(monkeypatch, *, client_id, claims):
    import fastmcp.server.dependencies as deps
    tok = types.SimpleNamespace(client_id=client_id, claims=claims)
    monkeypatch.setattr(deps, "get_access_token", lambda: tok)


def test_mcp_un_jeton_d_api_se_reconnait_positivement(monkeypatch):
    monkeypatch.setattr("oto_mcp.tenant_migration.alias_drain_armed", lambda: False)
    _jeton_mcp(monkeypatch, client_id="oto_api_token",
               claims={"sub": "u-1", "token_id": 9, "token_kind": "user"})
    assert garde_admin.courant() == garde_admin.Porteur(garde_admin.JETON, "u-1")
    _jeton_mcp(monkeypatch, client_id="oto_api_token",
               claims={"sub": "u-1", "token_id": 9, "token_kind": "delegation"})
    assert garde_admin.courant().nature == garde_admin.DELEGATION


def test_mcp_un_jwt_est_une_session(monkeypatch):
    monkeypatch.setattr("oto_mcp.tenant_migration.alias_drain_armed", lambda: False)
    _jeton_mcp(monkeypatch, client_id="https://client.example/cimd.json",
               claims={"sub": "id-logto", "aud": "x"})
    assert garde_admin.courant() == garde_admin.Porteur(
        garde_admin.SESSION, "id-logto", sub_logto="id-logto")


def test_mcp_hors_requete_aucun_porteur(monkeypatch):
    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token", lambda: None)
    assert garde_admin.courant() is None


# ── 6. Le mode ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valeur,attendu", [
    (None, "enforce"), ("", "enforce"), ("ENFORCE", "enforce"), ("report", "report"),
    ("off", "off"), ("of", "enforce"), ("disabled", "enforce")])
def test_le_mode_ferme_par_defaut(monkeypatch, valeur, attendu):
    if valeur is None:
        monkeypatch.delenv("OTO_ADMIN_MFA", raising=False)
    else:
        monkeypatch.setenv("OTO_ADMIN_MFA", valeur)
    assert garde_admin.mode() == attendu


@pytest.mark.parametrize("valeur", ["report", "off"])
def test_report_et_off_ne_retirent_rien(roles, facteurs, porteur, monkeypatch, valeur):
    monkeypatch.setenv("OTO_ADMIN_MFA", valeur)
    porteur(garde_admin.JETON)
    assert access.is_super_admin(OP)


# ── 7. Tripwire : les surfaces d'administration ──────────────────────────────

def _branches_a_plancher(regle) -> list:
    if _authz.platform_floor(regle) in ("operator", "super") and not getattr(
            regle, "autz_branches", ()):
        return [regle]
    return [b for r in getattr(regle, "autz_branches", ()) for b in _branches_a_plancher(r)]


def _entrees(cap) -> list:
    """`None`, ou une entrée par valeur d'`op` quand l'Input en déclare (une règle
    `ADMIN_BY_OP` choisit sa branche par l'op et n'expose pas ses branches)."""
    champ = cap.Input.model_fields.get("op")
    valeurs = typing.get_args(champ.annotation) if champ is not None else ()
    if valeurs and all(isinstance(v, str) for v in valeurs):
        return [types.SimpleNamespace(op=v) for v in valeurs]
    return [None]


def test_toute_regle_a_plancher_refuse_un_operateur_sous_jeton(roles, facteurs, porteur):
    """Toute branche d'autz qui exige un rôle plateforme, sur TOUT le registre servi,
    refuse un super_admin qui présente un jeton d'API — en le disant. Une règle neuve
    qui relirait le rôle ailleurs qu'au seam rougirait ici."""
    porteur(garde_admin.JETON)
    vues = set()
    for cap in registry.CAPABILITIES:
        for branche in _branches_a_plancher(cap.authz):
            for inp in _entrees(cap):
                try:
                    branche(RawCtx(sub=OP), inp)
                except AuthzDenied as e:
                    if e.code == "unsupported_op":
                        continue
                    assert e.code == garde_admin.CODE_SESSION, (cap.key, inp, e.code)
                    vues.add(cap.key)
                else:
                    pytest.fail(f"{cap.key} ({inp}) : un jeton d'API a passé la règle")
    assert len(vues) > 50, "le tripwire doit garder quelque chose"
    # Les surfaces nommées par l'audit sont dedans.
    admin = {c.key for c in registry.CAPABILITIES
             if (c.mcp or "").startswith("oto_admin_")
             or (getattr(c.rest, "path", "") or "").startswith("/api/admin/")}
    assert len(admin & vues) > 30, sorted(admin - vues)


def test_les_surfaces_admin_ne_lisent_le_role_qu_au_seam():
    """Aucun module ne compare un rôle plateforme LITTÉRAL hors du seam : le faire
    contournerait la garde (`role_effectif`). `activation.py` choisit des DESTINATAIRES
    d'e-mail, pas un droit."""
    import ast
    import pathlib
    racine = pathlib.Path(garde_admin.__file__).parent
    permis = {"access/scope.py", "garde_admin.py", "verrou_org.py", "activation.py"}
    fautes = []
    for f in racine.rglob("*.py"):
        rel = f.relative_to(racine).as_posix()
        if rel in permis or "/migrations/" in rel:
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Compare):
                for c in [n.left, *n.comparators]:
                    elts = c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c]
                    if any(isinstance(x, ast.Constant) and x.value in ("admin", "super_admin")
                           for x in elts):
                        fautes.append(f"{rel}:{n.lineno}")
    assert not fautes, fautes


# ── 8. Par les vraies portes : adaptateur REST, adaptateur MCP, « en tant que » ─

from pydantic import BaseModel  # noqa: E402

from oto_mcp.capabilities._types import Capability, RestBinding  # noqa: E402


class _Vide(BaseModel):
    pass


def _cap_admin(regle=None) -> Capability:
    return Capability(key="test.garde_admin", handler=lambda ctx, inp: {"sub": ctx.sub},
                      Input=_Vide, authz=regle or _authz.PLATFORM_ADMIN,
                      mcp="oto_admin_test_garde",
                      rest=RestBinding("GET", "/api/admin/test-garde"))


class _VerifierJwt:
    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": OP})


@pytest.fixture
def porte_reelle(monkeypatch, roles, facteurs):
    """La vraie `_authenticate`, ses lectures en base doublées : un jeton `oto_` est
    celui de l'opérateur, un autre bearer est un JWT de l'opérateur."""
    from oto_mcp.api import base
    monkeypatch.setattr(base.db, "verify_api_token",
                        lambda t: {"sub": OP, "scopes": None, "token_id": 3,
                                   "token_kind": "user"})
    monkeypatch.setattr(base.garde_identite, "refus", lambda sub: None)
    monkeypatch.setattr(base.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(base.service_identity, "adresse_aux_services", lambda t: False)
    monkeypatch.setattr(base, "alias_drain_armed", lambda: False)
    yield base
    garde_admin.poser(None)


def _client_rest(base, cap):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.capabilities import _rest_adapter
    routes = _rest_adapter.make_routes(_VerifierJwt(), base._authenticate, base._json,
                                       base._json_error, lambda r: None, [cap])
    return TestClient(Starlette(routes=routes))


@pytest.mark.parametrize("bearer,types_,statut,code", [
    ("jwt.session", ["Totp"], 200, None),
    ("jwt.session", [], 403, garde_admin.CODE_MFA),
    ("oto_jeton", ["Totp"], 403, garde_admin.CODE_SESSION),
])
def test_rest_une_route_admin_exige_session_et_mfa(porte_reelle, facteurs, bearer, types_,
                                                    statut, code):
    facteurs["types"] = types_
    r = _client_rest(porte_reelle, _cap_admin()).get(
        "/api/admin/test-garde", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == statut, r.text
    if code:
        assert r.json()["error"] == code
    else:
        assert r.json()["sub"] == OP


@pytest.mark.parametrize("claims,client_id,types_,code", [
    ({"sub": OP}, "https://client.example/cimd.json", ["Totp"], None),
    ({"sub": OP}, "https://client.example/cimd.json", [], garde_admin.CODE_MFA),
    ({"sub": OP, "token_id": 3, "token_kind": "user"}, "oto_api_token", ["Totp"],
     garde_admin.CODE_SESSION),
])
def test_mcp_un_outil_admin_exige_session_et_mfa(roles, facteurs, monkeypatch, claims,
                                                  client_id, types_, code):
    from oto_mcp.capabilities import _mcp_adapter
    from oto_mcp.mcp_errors import McpError
    monkeypatch.setattr("oto_mcp.tenant_migration.alias_drain_armed", lambda: False)
    monkeypatch.setattr("oto_mcp.auth.hooks.alias_drain_armed", lambda: False,
                        raising=False)
    _jeton_mcp(monkeypatch, client_id=client_id, claims=claims)
    facteurs["types"] = types_
    garde_admin.poser(None)
    outil = _mcp_adapter._make_tool(_cap_admin())
    fn = getattr(outil, "fn", outil)
    if code is None:
        assert asyncio.run(fn())["sub"] == OP
    else:
        with pytest.raises(McpError) as e:
            asyncio.run(fn())
        assert "second factor" in str(e.value) or "interactive" in str(e.value)


def _client_vue(base):
    """`ViewAsMiddleware` réel devant une route qui dit ce que la vue a appliqué."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from oto_mcp import session_org
    from oto_mcp.api import routes as api_routes

    async def sonde(request):
        return JSONResponse({
            "vue": request.scope.get(api_routes.CLE_VIEW_AS_APPLIQUE),
            "ecriture": bool(request.scope.get(api_routes.CLE_VIEW_AS_ECRITURE)),
            "ecriture_acceptee": bool(session_org.view_as_write_accepted())})
    app = Starlette(routes=[Route("/api/sonde", sonde, methods=["GET", "POST"])])
    return TestClient(api_routes.ViewAsMiddleware(app, _VerifierJwt()))


@pytest.mark.parametrize("bearer,types_,ouverte", [
    ("jwt.session", ["Totp"], True),
    ("jwt.session", [], False),
    ("oto_jeton", ["Totp"], False),
])
def test_voir_et_ecrire_en_tant_que_exigent_session_et_mfa(porte_reelle, facteurs, roles,
                                                           monkeypatch, bearer, types_,
                                                           ouverte):
    """L'écriture « en tant que » d'un super_admin (accusée par `X-Oto-View-As-Write`)
    n'est servie qu'à une session avec second facteur ; sinon la consultation n'est
    même pas ouverte (le porteur n'est plus opérateur, et il n'est pas org_admin)."""
    roles["cible"] = "member"
    facteurs["types"] = types_
    r = _client_vue(porte_reelle).post(
        "/api/sonde", json={"op": "set"},
        headers={"Authorization": f"Bearer {bearer}", "X-Oto-View-As": "cible",
                 "X-Oto-View-As-Write": "1"})
    if ouverte:
        assert r.status_code == 200, r.text
        assert r.json() == {"vue": "cible", "ecriture": True, "ecriture_acceptee": True}
    else:
        assert r.status_code in (400, 403), r.text
        assert r.json().get("vue") is None

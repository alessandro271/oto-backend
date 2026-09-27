"""Comptes Google PARTAGÉS par l'org ou l'équipe (2026-09-27).

Un admin d'org (ou un chef d'équipe) connecte UN compte Google au nom de tous — une
boîte partagée, un agenda d'équipe. Ce banc tient :

1. le CONSENTEMENT — seul un admin du scope le démarre ; le state signé porte le scope
   (et l'équipe), le callback range le jeton sous l'org ou l'équipe, jamais sous le
   membre qui a cliqué ;
2. la RÉSOLUTION — le compte du membre d'abord, puis l'équipe active, puis l'org ; un
   compte nommé se trouve où il vit ; le refresh et la santé s'écrivent sur l'entité
   qui porte la ligne ;
3. la GESTION — retirer ou choisir le défaut d'un compte partagé est un geste d'admin ;
4. la LECTURE — statut, lien de carte et identités voient les comptes partagés.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")

from oto_mcp import access, credentials_store, providers, roles  # noqa: E402
from oto_mcp.auth import google as G  # noqa: E402
from oto_mcp.capabilities import federated_oauth as fo  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx  # noqa: E402
from oto_mcp.connectors import flow as connector_flow  # noqa: E402
from oto_mcp.connectors import identities  # noqa: E402
from oto_mcp.connectors import link as connector_link  # noqa: E402

ORG, GROUP = 7, 9
ALL = " ".join(G.SCOPES)


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: GROUP)
    monkeypatch.setattr(credentials_store, "get_editor_app", lambda c, k: None)


def _admins(monkeypatch, org=False, group=False):
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org_id: org)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: group)


def _q(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ─── 1. le consentement ───────────────────────────────────────────────────────

def test_le_state_porte_le_scope_et_lequipe():
    etat = G.make_state("u", ORG, "", "gmail", "group", GROUP)
    assert G.verify_state(etat) == ("u", ORG, "", "gmail", "group", GROUP)
    assert G.verify_state(G.make_state("u", ORG, "", "drive", "org"))[4:] == ("org", None)
    # Un state d'avant les comptes partagés : le membre, comme alors.
    assert G.verify_state(G.make_state("u", ORG))[4:] == ("member", None)
    # Une équipe sans identifiant ne passe pas, même bien signée.
    assert G.verify_state(G.make_state("u", ORG, "", "gmail", "group", None)) is None


def test_seul_un_admin_de_lorg_connecte_un_compte_de_lorg(monkeypatch):
    _admins(monkeypatch, org=False)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="gmail", scope="org")
    _admins(monkeypatch, org=True)
    etat = _q(G.build_auth_url("u", connector="gmail", scope="org"))["state"]
    assert G.verify_state(etat)[4:] == ("org", None)


def test_seul_un_chef_dequipe_connecte_un_compte_dequipe(monkeypatch):
    _admins(monkeypatch, group=False)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="calendar", scope="group")
    _admins(monkeypatch, group=True)
    etat = _q(G.build_auth_url("u", connector="calendar", scope="group"))["state"]
    assert G.verify_state(etat)[4:] == ("group", GROUP)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="calendar", scope="group")


@pytest.mark.asyncio
async def test_le_flux_refuse_un_non_admin_en_nommant_le_refus(monkeypatch):
    _admins(monkeypatch, org=False)

    class _C:
        sub = "u"
    with pytest.raises(AuthzDenied) as e:
        await connector_flow.start("drive", _C(), {"scope": "org"})
    assert e.value.status == 403 and e.value.code == "scope_forbidden"
    with pytest.raises(AuthzDenied) as e:
        await connector_flow.start("drive", _C(), {"scope": "planete"})
    assert e.value.code == "invalid_scope"


def test_le_jeton_partage_se_range_sous_lorg_ou_lequipe(monkeypatch):
    vus = []
    monkeypatch.setattr(G, "_fetch_email", lambda tok, scopes=(): "hello@acme.test")
    monkeypatch.setattr(G.db, "set_shared_google_oauth",
                        lambda scope, target, **k: vus.append((scope, target, k["set_by"])))
    monkeypatch.setattr(G.db, "set_google_oauth",
                        lambda *a, **k: pytest.fail("jamais sous le membre qui a cliqué"))
    jeton = {"refresh_token": "rt", "access_token": "at", "expires_in": 3600, "scope": ALL}
    G.persist_token("u", ORG, jeton, scope="org")
    G.persist_token("u", ORG, jeton, scope="group", group_id=GROUP)
    assert vus == [("org", ORG, "u"), ("group", GROUP, "u")]


# ─── 2. la résolution ─────────────────────────────────────────────────────────

def _row(email, scopes=ALL, token="AT"):
    return {"google_email": email, "refresh_token": "RT", "access_token": token,
            "expires_at": "2999-01-01T00:00:00+00:00", "scopes": scopes, "client_id": None}


def _coffre(monkeypatch, membre=None, groupe=None, org=None):
    def membre_get(sub, org_id, account=None):
        return membre if membre and account in (None, membre["google_email"]) else None

    def partage_get(scope, target, account=None):
        row = {"group": groupe, "org": org}[scope]
        return row if row and account in (None, row["google_email"]) else None
    monkeypatch.setattr(G.db, "get_google_oauth", membre_get)
    monkeypatch.setattr(G.db, "get_shared_google_oauth", partage_get)


def test_le_membre_dabord_puis_lequipe_puis_lorg(monkeypatch):
    _coffre(monkeypatch, membre=_row("moi@x.test"), groupe=_row("team@x.test"),
            org=_row("hello@x.test"))
    assert G.credentials_for("u").token == "AT"
    assert G._resolve_row("u", ORG, None)[1] == ("member", None)
    _coffre(monkeypatch, groupe=_row("team@x.test"), org=_row("hello@x.test"))
    assert G._resolve_row("u", ORG, None) == (_row("team@x.test"), ("group", GROUP))
    _coffre(monkeypatch, org=_row("hello@x.test"))
    assert G._resolve_row("u", ORG, None)[1] == ("org", ORG)
    # Un compte NOMMÉ se trouve où il vit.
    _coffre(monkeypatch, membre=_row("moi@x.test"), org=_row("hello@x.test"))
    assert G._resolve_row("u", ORG, "hello@x.test")[1] == ("org", ORG)


def test_le_refresh_et_la_sante_secrivent_sur_lentite_partagee(monkeypatch):
    _coffre(monkeypatch, org=_row("hello@x.test", token=None))
    maj, sante = [], []
    monkeypatch.setattr(G.db, "update_shared_google_access_token",
                        lambda scope, target, email, tok, exp: maj.append((scope, target, email, tok)))
    monkeypatch.setattr(G.db, "update_google_access_token",
                        lambda *a, **k: pytest.fail("pas la ligne d'un membre"))
    from oto_mcp.connectors import health
    monkeypatch.setattr(health, "record_health", lambda prov, scope, ok, err: sante.append(scope))

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "AT-NEUF", "expires_in": 3600}

        def raise_for_status(self):
            pass
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _OK())
    creds = G.credentials_for("u", service="gmail")
    assert creds.token == "AT-NEUF"
    assert maj == [("org", ORG, "hello@x.test", "AT-NEUF")]
    assert sante == [("org", str(ORG), "hello@x.test")]


# ─── 3. la gestion ────────────────────────────────────────────────────────────

def test_retirer_un_compte_partage_est_un_geste_dadmin(monkeypatch):
    retraits = []
    monkeypatch.setattr(G.db, "list_shared_google_accounts",
                        lambda scope, target: [{"google_email": "hello@x.test"}])
    monkeypatch.setattr(G.db, "get_shared_google_oauth", lambda *a, **k: None)
    monkeypatch.setattr(G.db, "delete_shared_google_oauth",
                        lambda scope, target, account=None: retraits.append((scope, target, account)))
    _admins(monkeypatch, org=False)
    with pytest.raises(AuthzDenied) as e:
        fo._google_revoke(ResolvedCtx(sub="u"), fo.GoogleRevokeInput(account="hello@x.test", scope="org"))
    assert e.value.code == "scope_forbidden" and retraits == []
    _admins(monkeypatch, org=True)
    fo._google_revoke(ResolvedCtx(sub="u"), fo.GoogleRevokeInput(account="hello@x.test", scope="org"))
    assert retraits == [("org", ORG, "hello@x.test")]


def test_le_defaut_partage_se_choisit_par_un_admin(monkeypatch):
    monkeypatch.setattr(G.db, "set_default_shared_google_account",
                        lambda scope, target, account: (scope, target) == ("group", GROUP))
    _admins(monkeypatch, group=True)
    out = fo._google_set_default(ResolvedCtx(sub="u"),
                                 fo.GoogleDefaultInput(account="team@x.test", scope="group"))
    assert out == {"ok": True, "default": "team@x.test"}
    _admins(monkeypatch, group=False)
    with pytest.raises(AuthzDenied):
        fo._google_set_default(ResolvedCtx(sub="u"),
                               fo.GoogleDefaultInput(account="team@x.test", scope="group"))


# ─── 4. la lecture ────────────────────────────────────────────────────────────

def _partages(monkeypatch):
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: [
        {"google_email": f"{scope}@x.test", "is_default": True, "scopes": ALL,
         "granted_at": None, "scope": scope, "target_id": target}])
    monkeypatch.setattr(G.db, "list_google_accounts", lambda sub, org: [])


def test_le_statut_montre_les_comptes_partages(monkeypatch):
    _partages(monkeypatch)
    out = fo._google_status(ResolvedCtx(sub="u"), fo.OAuthStatusInput())
    assert out["accounts"] == []
    assert [(a["email"], a["scope"]) for a in out["shared"]] == [
        ("group@x.test", "group"), ("org@x.test", "org")]
    assert out["shared"][0]["services"] == list(G.SERVICES)


def test_la_carte_dun_service_est_reliee_par_un_compte_partage(monkeypatch):
    _partages(monkeypatch)
    assert connector_link.state("drive", "u").linked is True
    assert connector_link.state("drive", "u").accounts == 2


def test_les_identites_proposent_les_comptes_partages_etiquetes(monkeypatch):
    _partages(monkeypatch)
    ids = identities.list_identities("u", "gmail")
    assert [i["id"] for i in ids] == ["group@x.test", "org@x.test"]
    assert all(i["is_default"] is False and "partagé" in i["label"] for i in ids)


def test_le_compte_google_accepte_le_palier_org_et_ses_services_non():
    assert "byo_org" in providers.REGISTRY["google"].auth_modes
    providers.require_credential("org", "google")          # ne lève pas
    providers.require_credential("group", "google")
    with pytest.raises(ValueError):
        providers.require_credential("org", "drive")       # délégué : pas de clé à lui

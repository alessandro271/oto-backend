"""L'app OAuth DÉDIÉE d'une org ou d'une équipe : pose REST seule, lecture sans secret.

Bancs SANS base : le coffre est doublé par un dictionnaire ; le rangement réel (scope
plateforme, préfixe `app:`) et son invisibilité pour la cascade sont éprouvés par
`test_access_level_live.py`.
"""
from __future__ import annotations

import datetime as dt

import pytest

from oto_mcp import credentials_store, org_store
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import oauth_app as cap
from oto_mcp.capabilities.registry import CAPABILITIES

ORG, EQUIPE = 3, 8


@pytest.fixture
def coffre(monkeypatch):
    rangees: dict = {}
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid, "name": "O"})

    def _set(t, i, c, fields, set_by=None):
        rangees[(t, i, c)] = {"fields": dict(fields), "set_by": set_by,
                              "set_at": dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(credentials_store, "set_dedicated_app", _set)
    monkeypatch.setattr(credentials_store, "get_dedicated_app",
                        lambda t, i, c: rangees.get((t, i, c)))
    monkeypatch.setattr(credentials_store, "clear_dedicated_app",
                        lambda t, i, c: rangees.pop((t, i, c), None) is not None)
    return rangees


CTX = ResolvedCtx(sub="admin-x", org_id=ORG, group_id=EQUIPE)


def test_aucune_face_mcp():
    """Un secret brut ne passe jamais en argument d'outil."""
    for c in CAPABILITIES:
        if c.key.startswith("connectors.oauth_app."):
            assert c.mcp is None, c.key


def test_poser_puis_lire_ne_rend_jamais_le_secret(coffre):
    out = cap._org_set(CTX, cap.OrgOAuthAppSetInput(
        org_id=ORG, name="zoho", client_id=" 1000.ORG ", client_secret="s3cret",
        data_center="EU"))
    assert coffre[("org", ORG, "zoho")]["fields"] == {
        "client_id": "1000.ORG", "client_secret": "s3cret", "data_center": "eu"}
    lu = cap._org_get(CTX, cap.OrgOAuthAppInput(org_id=ORG, name="zoho"))
    assert lu == out
    assert lu["configured"] is True and lu["data_center"] == "eu"
    assert set(lu["read_fingerprints"]) == {"client_id", "client_secret"}
    assert all(len(v) == 4 for v in lu["read_fingerprints"].values())
    texte = repr(lu)
    assert "s3cret" not in texte and "1000.ORG" not in texte
    assert lu["read_set_by"] == "admin-x" and lu["read_set_at"].startswith("2026-10-14")


def test_rien_de_pose_se_dit_configured_false(coffre):
    lu = cap._group_get(CTX, cap.GroupOAuthAppInput(group_id=EQUIPE, name="zohodesk"))
    assert lu["configured"] is False and lu["read_fingerprints"] == {}
    assert (lu["scope"], lu["group_id"], lu["org_id"]) == ("group", EQUIPE, ORG)


def test_retirer_est_idempotent(coffre):
    cap._group_set(CTX, cap.GroupOAuthAppSetInput(
        group_id=EQUIPE, name="zoho", client_id="a", client_secret="b", data_center="com"))
    assert cap._group_clear(CTX, cap.GroupOAuthAppInput(
        group_id=EQUIPE, name="zoho"))["deleted"] is True
    assert cap._group_clear(CTX, cap.GroupOAuthAppInput(
        group_id=EQUIPE, name="zoho"))["deleted"] is False


@pytest.mark.parametrize("nom, statut, code", [
    ("inconnu", 404, "unknown_connector"),
    ("serper", 400, "oauth_app_unsupported"),     # aucun flux ne lirait l'app
])
def test_un_connecteur_qui_ne_lit_pas_d_app_est_refuse(coffre, nom, statut, code):
    with pytest.raises(AuthzDenied) as e:
        cap._org_set(CTX, cap.OrgOAuthAppSetInput(
            org_id=ORG, name=nom, client_id="a", client_secret="b", data_center="eu"))
    assert (e.value.status, e.value.code) == (statut, code)
    assert not coffre


@pytest.mark.parametrize("dc", [None, "", "mars"])
def test_une_app_zoho_porte_sa_region(coffre, dc):
    with pytest.raises(AuthzDenied) as e:
        cap._org_set(CTX, cap.OrgOAuthAppSetInput(
            org_id=ORG, name="zoho", client_id="a", client_secret="b", data_center=dc))
    assert e.value.code == "invalid_oauth_app"
    assert not coffre

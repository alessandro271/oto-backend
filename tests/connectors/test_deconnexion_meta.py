"""Déconnexion OAuth de meta_ads et instagram_meta (oto-backend#1241).

Les deux connecteurs se connectaient par le geste générique
(`POST /api/me/connectors/{name}/connect`) sans pouvoir se déconnecter : seul google
avait déclaré `me.connector_disconnect`. Ce fichier MORD sur :

- **meta_ads** révoque chez Meta (`DELETE /{user-id}/permissions`, jeton dans
  l'EN-TÊTE, jamais dans l'URL), puis retire la ligne MEMBRE du coffre ;
- un **échec de révocation** retire la ligne quand même (contrainte 2 de
  `oauth_status.py`) mais se JOURNALISE — jamais tu ;
- **instagram_meta** retire la ligne sans appel réseau (aucune révocation documentée
  par l'API Instagram Login) ;
- **idempotent** : rien à retirer ⟹ `disconnected: false`, aucune révocation ;
- le **catalogue** l'annonce : `connect.disconnect` vaut `true` pour ces deux-là et
  pour google, `false` pour un connecteur à flux qui n'a pas déclaré le geste.
"""
from __future__ import annotations

import logging

import pytest
import requests
from fastmcp import FastMCP

from _datastore_rest import call, stub_authz

from oto_mcp import access, credentials_store
from oto_mcp.capabilities.connectors import oauth_status  # noqa: F401 — déclarations

pytest.importorskip("oto.tools.meta_ads")

_JETON = "jeton-meta-de-test"


@pytest.fixture
def coffre(monkeypatch):
    """Une ligne MEMBRE par connecteur, lue et effacée en mémoire."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    lignes = {
        ("meta_ads", credentials_store.member_id(42, "u-1")): {
            "secret": _JETON, "meta": {"user_id": "1234567890"}},
        ("instagram_meta", credentials_store.member_id(42, "u-1")): {
            "secret": _JETON, "meta": {"user_id": "17841400000000000"}},
    }

    def _lire(entity_type, entity_id, connector, account=""):
        assert entity_type == credentials_store.MEMBER
        return lignes.get((connector, entity_id))

    def _effacer(entity_type, entity_id, connector, conn=None, account=""):
        assert entity_type == credentials_store.MEMBER
        return lignes.pop((connector, entity_id), None) is not None

    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _lire)
    monkeypatch.setattr(credentials_store, "clear_credential", _effacer)
    return lignes


@pytest.fixture
def graph(monkeypatch):
    """Les appels DELETE vers Meta, enregistrés ; `graph.echec` les fait lever."""
    class _Graph:
        appels: list = []
        echec: Exception | None = None

    g = _Graph()
    g.appels = []

    class _Reponse:
        def raise_for_status(self):
            return None

    def _delete(url, headers=None, timeout=None, **kw):
        g.appels.append({"url": url, "headers": dict(headers or {}), "kw": kw})
        if g.echec is not None:
            raise g.echec
        return _Reponse()

    monkeypatch.setattr(requests, "delete", _delete)
    return g


def test_meta_ads_revoque_chez_meta_puis_efface(coffre, graph):
    code, out = call("me.connector_disconnect", path_params={"name": "meta_ads"})
    assert (code, out) == (200, {"ok": True, "disconnected": True})
    assert len(graph.appels) == 1
    appel = graph.appels[0]
    assert appel["url"].startswith("https://graph.facebook.com/")
    assert appel["url"].endswith("/1234567890/permissions")
    assert appel["headers"] == {"Authorization": f"Bearer {_JETON}"}
    assert _JETON not in appel["url"] and not appel["kw"].get("params")
    assert ("meta_ads", credentials_store.member_id(42, "u-1")) not in coffre


def test_un_echec_de_revocation_efface_quand_meme_et_se_journalise(coffre, graph, caplog):
    graph.echec = requests.ConnectionError("graph injoignable")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.auth.meta_ads"):
        code, out = call("me.connector_disconnect", path_params={"name": "meta_ads"})
    assert (code, out) == (200, {"ok": True, "disconnected": True})
    assert ("meta_ads", credentials_store.member_id(42, "u-1")) not in coffre
    lignes = [r.getMessage() for r in caplog.records if r.name == "oto_mcp.auth.meta_ads"]
    assert any("revocation failed" in m and "ConnectionError" in m for m in lignes), lignes
    assert not any(_JETON in m for m in lignes)


def test_instagram_meta_efface_sans_appel_reseau(coffre, graph):
    code, out = call("me.connector_disconnect", path_params={"name": "instagram_meta"})
    assert (code, out) == (200, {"ok": True, "disconnected": True})
    assert graph.appels == []
    assert ("instagram_meta", credentials_store.member_id(42, "u-1")) not in coffre


@pytest.mark.parametrize("nom", ["meta_ads", "instagram_meta"])
def test_rien_a_deconnecter_est_idempotent(coffre, graph, nom):
    coffre.clear()
    code, out = call("me.connector_disconnect", path_params={"name": nom})
    assert (code, out) == (200, {"ok": True, "disconnected": False})
    assert graph.appels == []


def test_le_catalogue_annonce_la_deconnexion():
    """`connect.disconnect` : ce que lit le front pour afficher « Déconnecter ». Banc
    chargé comme le boot (cf. `test_carte_connecteur_declaree.py`)."""
    from oto_mcp.providers import public_catalog
    from oto_mcp.tools import register_all

    register_all(FastMCP("deconnexion-meta-probe"))
    from oto_mcp.api import routes as api_routes  # noqa: F401

    cat = {c["name"]: c for c in public_catalog()}
    for nom in ("google", "meta_ads", "instagram_meta"):
        assert cat[nom]["connect"]["disconnect"] is True, nom
    sans = [n for n, c in cat.items()
            if c.get("connect") and n not in ("google", "meta_ads", "instagram_meta")]
    assert sans, "aucun autre connecteur à flux : le témoin ne mesure rien"
    assert all(cat[n]["connect"]["disconnect"] is False for n in sans)

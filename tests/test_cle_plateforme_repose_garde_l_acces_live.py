"""Re-poser une clé plateforme sous le même libellé garde l'accès accordé (#1344).

La re-pose depuis la console (`POST /api/admin/platform-keys`, même libellé) est une
rotation du secret : l'org accordée doit être servie avant ET après, par la
résolution réelle (`resolve._resolve_credential_impl`), que l'accord vive sur la ligne
du coffre (`share_down`, connecteur non chaîné) ou sur une arête (`grants_chain`).
Banc de non-régression : le signal d'origine (une clé payante à deux champs, re-posée
puis refusée « No … key configured for you ») ne s'est pas reproduit sur ce chemin.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from _base_jetable import base_jetable


@pytest.fixture
def base(pg_dsn, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    with base_jetable(pg_dsn):
        from oto_mcp.db import init_db
        init_db()
        yield


def _servi(provider: str):
    from oto_mcp.access import resolve

    return resolve._resolve_credential_impl(provider, "auto", "membre", check_usage=False)


@pytest.fixture
def membre_de_l_org_42(monkeypatch):
    from oto_mcp import access

    monkeypatch.setattr(access, "current_org", lambda sub: 42)


@pytest.mark.parametrize("provider", ["fullenrich", "apollo", "forager"])
def test_re_poser_garde_l_acces_de_l_org_accordee(base, membre_de_l_org_42, provider):
    """Chaîné (`fullenrich`, `apollo` : l'accord est une arête) et non chaîné
    (`forager` : l'accord est dans `share_down`)."""
    from oto_mcp import credentials_store

    credentials_store.set_platform_key("prod", provider, "k-1", set_by="op")
    credentials_store.platform_grant(provider, "org:42")
    assert _servi(provider).secret == "k-1"

    credentials_store.set_platform_key("prod", provider, "k-2", set_by="op")

    servi = _servi(provider)
    assert servi.is_platform and servi.secret == "k-2"


def test_re_poser_par_la_console_une_cle_a_deux_champs(base, membre_de_l_org_42):
    """Le geste du signal : la console pose les DEUX champs sous le même libellé."""
    from oto_mcp import credentials_store
    from oto_mcp.capabilities import api_tokens

    def poser(mot_de_passe: str) -> None:
        api_tokens._keys_create(SimpleNamespace(sub="op"), SimpleNamespace(
            provider="dataforseo", label="prod", api_key="",
            fields={"login": "l", "password": mot_de_passe}))

    poser("p-1")
    credentials_store.platform_grant("dataforseo", "org:42")
    assert _servi("dataforseo").is_platform

    poser("p-2")

    inst, = credentials_store.list_platform_instances("dataforseo")
    assert inst["share_mode"] == "closed" and inst["share_down"] == ["org:42"]
    assert _servi("dataforseo").is_platform

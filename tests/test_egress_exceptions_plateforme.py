"""Les exceptions nommées de la garde d'egress (`OTO_EGRESS_ALLOW`) sont celles de
l'OPÉRATEUR : elles valent pour un identifiant du palier plateforme, jamais pour celui
d'un membre, d'une équipe, d'une org ou d'un tenant (`egress_transport.exceptions_pour`).

Un test par contrôle précoce (`egress.check_url`) qui les appliquait à tout identifiant :
la connexion WordPress, la sonde et les outils WordPress, la sonde et les outils
LightOn, la carte API (`base_url` et `token_url`). Même destination déclarée dans
chaque cas : refusée pour un membre ou une org, acceptée pour la plateforme. Aucun
accès réseau : la résolution passe par la couture `egress.resolved_addresses`, et les
clients de la lib sont remplacés là où le contrôle laisserait passer.
"""
from __future__ import annotations

import pytest

from oto_mcp import egress, egress_transport

INTERNE = "127.0.0.1"
MEMBRE, ORG, PLATEFORME = ("user", "u", ""), ("org", "1", ""), ("platform", "cle", "")


@pytest.fixture(autouse=True)
def destination_declaree(monkeypatch):
    """Tout hôte résout vers une adresse interne DÉCLARÉE par l'opérateur."""
    monkeypatch.setenv(egress.ALLOW_VAR, f"declaree={INTERNE}:443")
    monkeypatch.setattr(egress, "resolved_addresses", lambda hote, port: {INTERNE})


class _Client:
    """Un client de la lib qui ne sort pas : construit = le contrôle a laissé passer."""
    def __init__(self, *a, **k):
        pass

    def me(self):
        return {"id": 1}

    def list_workspaces(self):
        return []


def test_seul_le_palier_plateforme_herite_des_exceptions():
    assert egress_transport.exceptions_pour("platform") is True
    for palier in ("user", "group", "org", "tenant", None, ""):
        assert egress_transport.exceptions_pour(palier) is False, palier


def test_la_connexion_wordpress_n_applique_pas_les_exceptions():
    """Le flux de connexion pose l'identifiant d'un membre, d'une équipe ou d'une org,
    jamais de la plateforme."""
    from oto_mcp.auth import wordpress as wp_auth
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        wp_auth.check_site("https://wp.exemple.test")


def test_la_sonde_wordpress_suit_l_origine_de_l_identifiant(monkeypatch):
    from oto_mcp.tools import wordpress
    monkeypatch.setattr("oto.tools.wordpress.WordPressClient", _Client)
    fields = {"site_url": "https://wp.exemple.test", "username": "u",
              "application_password": "p"}
    for instance in (MEMBRE, ORG, None):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            wordpress._verify(fields, instance=instance)
    wordpress._verify(fields, instance=PLATEFORME)


def test_les_outils_wordpress_n_appliquent_pas_les_exceptions(monkeypatch):
    """Les outils résolvent par `resolve_credential_fields`, qui ne sert jamais le
    palier plateforme."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import wordpress
    monkeypatch.setattr(wordpress.access, "resolve_credential_fields",
                        lambda provider, account=None: {
                            "site_url": "https://wp.exemple.test", "username": "u",
                            "application_password": "p"})
    with pytest.raises(McpError, match="destination refusée"):
        wordpress._client()


def test_la_sonde_lighton_suit_l_origine_de_l_identifiant(monkeypatch):
    from oto_mcp.tools import lighton
    monkeypatch.setattr("oto.tools.lighton.LightOnClient", _Client)
    fields = {"api_key": "k", "base_url": "https://lighton.exemple.test"}
    for instance in (MEMBRE, ORG, None):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            lighton._verify(fields, instance=instance)
    lighton._verify(fields, instance=PLATEFORME)


def test_les_outils_lighton_n_appliquent_pas_les_exceptions(monkeypatch):
    import asyncio
    from fastmcp import FastMCP
    from oto_mcp.tools import lighton
    monkeypatch.setattr(lighton.access, "resolve_credential_fields",
                        lambda provider, account=None: {
                            "api_key": "k", "base_url": "https://lighton.exemple.test"})
    m = FastMCP("t")
    lighton.register(m)
    outil = asyncio.run(m.get_tool("lighton_search")).fn
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        outil(query="x")


@pytest.mark.parametrize("champ,fields", [
    ("base_url", {"base_url": "https://api.exemple.test", "auth_mode": "bearer",
                  "token": "t"}),
    ("token_url", {"base_url": "https://api.exemple.test", "auth_mode": "oauth2",
                   "token_url": "https://jeton.exemple.test/token",
                   "client_id": "i", "client_secret": "s"}),
])
def test_la_carte_api_suit_l_origine_de_sa_ligne(monkeypatch, champ, fields):
    from oto_mcp.tools import api as tool_api
    if champ == "token_url":
        # `base_url` publique : seul `token_url` vise la destination déclarée.
        monkeypatch.setattr(egress, "resolved_addresses",
                            lambda hote, port: {"93.184.216.34"} if hote.startswith("api.")
                            else {INTERNE})
    for row in (MEMBRE, ORG, None):
        with pytest.raises(egress.EgressRefused, match=f"destination refusée(.|\\n)*{champ}"):
            tool_api.client_for(fields, row=row)
    tool_api.client_for(fields, row=PLATEFORME)

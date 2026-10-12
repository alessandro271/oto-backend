"""F-051 — une org ne peut plus épuiser seule une clé plateforme partagée.

Rien ne bornait le débit d'UNE org sur une clé commune : le quota du jour est par
personne et par jour (souvent levé), et une org en rafale consommait le plafond du
fournisseur pour toutes. `access/platform_rate.py` compte par (org, connecteur) et par
fenêtre fixe, en base ; `report` (défaut) journalise, `enforce` refuse.
"""
from __future__ import annotations

import logging
import os

import pytest

from _base_jetable import base_jetable


# ── Réglages (sans base) ──────────────────────────────────────────────────────

def test_le_mode_par_defaut_est_report_et_une_valeur_inconnue_aussi(monkeypatch):
    from oto_mcp.access import platform_rate

    monkeypatch.delenv("OTO_PLATFORM_KEY_ORG_RATE", raising=False)
    assert platform_rate.mode() == "report"
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "nimporte")
    assert platform_rate.mode() == "report"
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", " Enforce ")
    assert platform_rate.mode() == "enforce"


def test_limite_commune_et_par_connecteur(monkeypatch):
    from oto_mcp.access import platform_rate

    monkeypatch.delenv("OTO_PLATFORM_KEY_ORG_RATE_LIMIT", raising=False)
    monkeypatch.delenv("OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES", raising=False)
    assert platform_rate.limit_for("aiark") == platform_rate.DEFAULT_LIMIT
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE_LIMIT", "7")
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES", "serper=600, aiark=0")
    assert platform_rate.limit_for("serper") == 600
    assert platform_rate.limit_for("aiark") == 0
    assert platform_rate.limit_for("hunter") == 7


def test_off_ne_touche_pas_la_base(monkeypatch):
    from oto_mcp.access import platform_rate

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "off")

    def interdit(*a, **k):
        raise AssertionError("aucun compte en mode off")

    monkeypatch.setattr(platform_rate.db_rate, "compter", interdit)
    platform_rate.enforce("aiark", "s", 42)


def test_un_compteur_casse_sert_l_appel_et_le_dit(monkeypatch, caplog):
    from oto_mcp.access import platform_rate

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "enforce")

    def casse(*a, **k):
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(platform_rate.db_rate, "compter", casse)
    with caplog.at_level(logging.ERROR):
        platform_rate.enforce("aiark", "s", 42)
    assert "counter failed" in caplog.text


# ── Sur une base réelle ───────────────────────────────────────────────────────

@pytest.fixture
def base(pg_dsn, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE_LIMIT", "3")
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S", "3600")
    monkeypatch.delenv("OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES", raising=False)
    with base_jetable(pg_dsn):
        from oto_mcp.db import init_db
        init_db()
        yield


def test_le_compteur_est_par_org_et_par_connecteur(base):
    from oto_mcp.db import platform_key_rate as db_rate

    assert [db_rate.compter("org:42", "aiark", 3600) for _ in range(3)] == [1, 2, 3]
    assert db_rate.compter("org:77", "aiark", 3600) == 1
    assert db_rate.compter("org:42", "serper", 3600) == 1


def test_une_fenetre_neuve_repart_de_un(base):
    from oto_mcp.db import _connect
    from oto_mcp.db import platform_key_rate as db_rate

    db_rate.compter("org:42", "aiark", 60)
    db_rate.compter("org:42", "aiark", 60)
    with _connect() as conn:
        conn.execute("UPDATE platform_key_rate SET window_start = window_start "
                     "- interval '1 hour'")
    assert db_rate.compter("org:42", "aiark", 60) == 1


def test_enforce_refuse_l_org_en_rafale_et_sert_les_autres(base, monkeypatch):
    from oto_mcp.access import platform_rate
    from oto_mcp.mcp_errors import McpError

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "enforce")
    for _ in range(3):
        platform_rate.enforce("aiark", "s", 42)
    with pytest.raises(McpError) as e:
        platform_rate.enforce("aiark", "s", 42, hint=lambda: " in your org settings")
    err = e.value.error
    assert err.data["code"] == platform_rate.CODE
    assert "retry" in err.message and "your own `aiark` key" in err.message
    platform_rate.enforce("aiark", "autre", 77)          # une autre org : servie


def test_report_journalise_une_fois_et_ne_refuse_rien(base, monkeypatch, caplog):
    from oto_mcp.access import platform_rate

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "report")
    with caplog.at_level(logging.WARNING):
        for _ in range(6):
            platform_rate.enforce("aiark", "s", 42)
    lignes = [r for r in caplog.records if "platform-key rate" in r.getMessage()]
    assert len(lignes) == 1 and "org:42" in lignes[0].getMessage()


def test_sans_org_le_compte_est_celui_de_la_personne(base, monkeypatch):
    from oto_mcp.access import platform_rate
    from oto_mcp.mcp_errors import McpError

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "enforce")
    for _ in range(3):
        platform_rate.enforce("aiark", "solo", None)
    with pytest.raises(McpError):
        platform_rate.enforce("aiark", "solo", None)
    platform_rate.enforce("aiark", "autre-solo", None)


def test_la_resolution_reelle_applique_la_limite_au_palier_plateforme(base, monkeypatch):
    """Bout en bout : la clé plateforme est servie trois fois à l'org, refusée la
    quatrième ; une configuration de connexion (`check_usage=False`) ne compte pas."""
    from oto_mcp import credentials_store
    from oto_mcp.access import platform_rate, resolve, scope
    from oto_mcp.mcp_errors import McpError

    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "enforce")
    credentials_store.set_credential(credentials_store.PLATFORM, "commune", "aiark",
                                     "k-1", set_by="op")      # ouverte à tous
    monkeypatch.setattr(scope, "current_org", lambda sub: 42)

    for _ in range(5):
        resolve._resolve_credential_impl("aiark", "auto", "membre", check_usage=False)
    for _ in range(3):
        assert resolve._resolve_credential_impl("aiark", "auto", "membre").is_platform
    with pytest.raises(McpError) as e:
        resolve._resolve_credential_impl("aiark", "auto", "membre")
    assert e.value.error.data["code"] == platform_rate.CODE

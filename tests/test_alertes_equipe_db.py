"""Le courriel d'ÉQUIPE des alertes part aux admins de plateforme — sur SQL réel.

Ce qu'une doublure ne prouve pas, c'est la lecture des comptes (`db.users.
operateurs_de_plateforme`) : le rôle plateforme lu dans `users.role`, la pause
(`users.suspended_at`) qui écarte. Le passage de l'alerte d'agents bloqués est joué
de bout en bout sur ces comptes ; seuls l'épisode et le facteur sont doublés.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

_JOUR = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)    # 12:00 à Paris
_ORG = 8470


def _compte(sub, email, role, *, suspendu=False):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, role, suspended_at, suspended_by, "
            "suspended_reason) VALUES (%s, %s, %s, %s, %s, %s)",
            (sub, email, role, datetime.now(timezone.utc) if suspendu else None,
             "op-actif" if suspendu else None, "banc" if suspendu else None))


@pytest.fixture()
def comptes(live, monkeypatch):
    from oto_mcp.db._conn import _connect
    monkeypatch.delenv("OTO_MCP_ADMIN_SUB", raising=False)
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE sub LIKE 'eq-%%'")
    _compte("eq-op-actif", "op-actif@exemple.invalid", "admin")
    _compte("eq-op-pause", "op-pause@exemple.invalid", "super_admin", suspendu=True)
    _compte("eq-membre", "membre@exemple.invalid", "member")
    yield
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE sub LIKE 'eq-%%'")


def _alerte_sur_l_org(monkeypatch):
    """Un agent bloqué dans l'org `_ORG`, dont le simple membre est le client écrit ;
    le facteur range chaque courriel par destinataire."""
    from oto_mcp import alerte_agents_bloques as M
    from oto_mcp import db, email_templates, org_store, tenancy
    from oto_mcp.db import alertes_agents, episodes_agents
    lettres: list = []
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous",
                        lambda ids, *, vus_depuis: set())
    monkeypatch.setattr(alertes_agents, "agents_bloques", lambda seuil: [
        {"trigger_id": 1, "org_id": _ORG, "libelle": "agent 1", "travaux": 3,
         "depuis": "2026-10-20 06:00:00", "dernier": "2026-10-22 06:00:00",
         "motif": "HTTP 403"}])
    monkeypatch.setattr(alertes_agents, "marquer_annonces", lambda ids: len(ids))
    monkeypatch.setattr(db, "org_tenant_slug", lambda o: tenancy.primary_slug())
    monkeypatch.setattr(org_store, "list_org_members",
                        lambda o: [{"sub": "eq-membre", "org_role": "org_admin"}])
    monkeypatch.setattr(org_store, "org_front", lambda o: (None, None))
    monkeypatch.setattr(org_store, "get_org", lambda o: {"id": o, "name": f"org {o}"})
    monkeypatch.setattr(email_templates, "_envoyer",
                        lambda m, to, subject, html: lettres.append((to, subject)) or True)
    return M, lettres


def test_seul_l_admin_de_plateforme_actif_recoit_le_courriel_d_equipe(comptes, monkeypatch):
    from oto_mcp import alertes_equipe
    assert alertes_equipe.destinataires() == ["op-actif@exemple.invalid"]
    M, lettres = _alerte_sur_l_org(monkeypatch)
    bilan = M.balayer(_JOUR)
    equipe = [to for to, s in lettres if s.startswith("[équipe] ")]
    clients = [to for to, s in lettres if not s.startswith("[équipe] ")]
    assert equipe == ["op-actif@exemple.invalid"], (
        "l'admin en pause et le simple membre ne reçoivent pas le courriel d'équipe")
    assert clients == ["membre@exemple.invalid"]
    assert bilan["equipe"] == 1


def test_le_compte_d_amorcage_compte_comme_admin_de_plateforme(comptes, monkeypatch):
    """`OTO_MCP_ADMIN_SUB` est super_admin quel que soit `users.role` : la même règle
    que la porte `PLATFORM_ADMIN` des outils `oto_admin_*`."""
    from oto_mcp import alertes_equipe
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", "eq-membre")
    assert alertes_equipe.destinataires() == ["op-actif@exemple.invalid",
                                              "membre@exemple.invalid"]


def test_sans_admin_de_plateforme_erreur_et_le_client_recoit(live, monkeypatch, caplog):
    from oto_mcp import alertes_equipe
    from oto_mcp.db._conn import _connect
    monkeypatch.delenv("OTO_MCP_ADMIN_SUB", raising=False)
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE sub LIKE 'eq-%%'")
        conn.execute("UPDATE users SET role = 'member' WHERE role <> 'member'")
    _compte("eq-membre", "membre@exemple.invalid", "member")
    _compte("eq-op-pause", "op-pause@exemple.invalid", "admin", suspendu=True)
    assert alertes_equipe.destinataires() == []
    M, lettres = _alerte_sur_l_org(monkeypatch)
    with caplog.at_level("ERROR", logger="oto_mcp.alertes_equipe"):
        bilan = M.balayer(_JOUR)
    assert [to for to, _s in lettres] == ["membre@exemple.invalid"]
    assert bilan["envoyes"] == 1 and bilan["equipe"] == 0
    erreurs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(erreurs) == 1 and f"org {_ORG}" in erreurs[0]
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE sub LIKE 'eq-%%'")

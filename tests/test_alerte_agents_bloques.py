"""Prévenir l'admin quand un agent programmé est bloqué (signal 1168) — sans base.

Les propriétés qui font que ce passage répare quelque chose : un courriel PAR ORG, sous
la marque de l'org ; la marque posée APRÈS l'envoi, et seulement s'il est parti ; rien
pour une org d'un tenant tiers (comptée) ; rien aux heures calmes ; rien hors production
(la boucle se déclare `tiers=True`)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oto_mcp import alerte_agents_bloques as M

_JOUR = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)    # 12:00 à Paris
_NUIT = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc)    # 23:00 à Paris


def _agent(tid, org):
    return {"trigger_id": tid, "org_id": org, "libelle": f"agent {tid}", "travaux": 3,
            "depuis": "2026-10-20 06:00:00", "dernier": "2026-10-22 06:00:00",
            "motif": "HTTP 403 <Access denied>"}


@pytest.fixture()
def bureau(monkeypatch):
    from oto_mcp import db, email_templates, org_store, tenancy
    from oto_mcp.db import alertes_agents
    from oto_mcp.db import users as U

    etat = {"lettres": [], "marques": [], "envoi_ok": True,
            "agents": [_agent(1, 7), _agent(2, 7), _agent(3, 9), _agent(4, 11)]}
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    monkeypatch.setattr(alertes_agents, "agents_bloques", lambda seuil: list(etat["agents"]))
    monkeypatch.setattr(alertes_agents, "marquer_annonces",
                        lambda ids: etat["marques"].extend(ids) or len(ids))
    # L'org 9 est celle d'un tenant tiers.
    monkeypatch.setattr(db, "org_tenant_slug",
                        lambda o: "partenaire" if int(o) == 9 else tenancy.primary_slug())
    # L'org 11 n'a aucun admin joignable.
    monkeypatch.setattr(org_store, "list_org_members", lambda o: [] if o == 11 else [
        {"sub": "m", "org_role": "member"}, {"sub": "a", "org_role": "org_admin"}])
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "fr"})
    monkeypatch.setattr(org_store, "org_front", lambda o: (None, None))

    def _envoyer(m, to, subject, html):
        etat["lettres"].append((to, subject, html))
        return etat["envoi_ok"]
    monkeypatch.setattr(email_templates, "_envoyer", _envoyer)
    return etat


def test_un_courriel_par_org_a_son_admin_puis_la_marque(bureau):
    bilan = M.balayer(_JOUR)
    assert [(to, s) for to, s, _ in bureau["lettres"]] == [
        ("a@acme.test", "Un agent programmé est bloqué")]
    html = bureau["lettres"][0][2]
    assert "agent 1" in html and "agent 2" in html
    assert "&lt;Access denied&gt;" in html, "le motif est échappé"
    assert bureau["marques"] == [1, 2]
    assert bilan == {"orgs": 3, "envoyes": 1, "marques": 2, "tenant_tiers_ecartes": 1,
                     "sans_destinataire": 1}


def test_un_envoi_rate_ne_marque_rien(bureau):
    bureau["envoi_ok"] = False
    assert M.balayer(_JOUR)["envoyes"] == 0
    assert bureau["marques"] == []


def test_aux_heures_calmes_rien_ne_part(bureau):
    assert M.balayer(_NUIT) == {"heures_calmes": True}
    assert bureau["lettres"] == [] and bureau["marques"] == []


def test_l_org_d_un_tenant_tiers_n_est_jamais_ecrite(bureau):
    bureau["agents"] = [_agent(3, 9)]
    assert M.balayer(_JOUR)["tenant_tiers_ecartes"] == 1
    assert bureau["lettres"] == [] and bureau["marques"] == []


def test_un_tenant_illisible_ferme(bureau, monkeypatch):
    from oto_mcp import db

    def _casse(o):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(db, "org_tenant_slug", _casse)
    assert M.balayer(_JOUR)["envoyes"] == 0
    assert bureau["lettres"] == []


def test_la_boucle_est_declaree_tierce():
    from oto_mcp import boucles_de_fond
    b = next(b for b in boucles_de_fond.BOUCLES if b.nom == "alerte_agents_bloques")
    assert b.tiers is True
    assert b.fonction() is M.run_alerte_agents_loop

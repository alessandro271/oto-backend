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
_EQUIPE = "equipe@exemple.invalid"


def _agent(tid, org):
    return {"trigger_id": tid, "org_id": org, "libelle": f"agent {tid}", "travaux": 3,
            "depuis": "2026-10-20 06:00:00", "dernier": "2026-10-22 06:00:00",
            "motif": "HTTP 403 <Access denied>"}


@pytest.fixture()
def bureau(monkeypatch):
    from oto_mcp import db, email_templates, org_store, tenancy
    from oto_mcp.db import alertes_agents, episodes_agents
    from oto_mcp.db import users as U

    etat = {"lettres": [], "equipe": [], "marques": [], "envoi_ok": True,
            "agents": [_agent(1, 7), _agent(2, 7), _agent(3, 9), _agent(4, 11)]}
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    etat["soignes"] = set()
    etat["fenetres"] = []

    def _soignes(ids, *, vus_depuis):
        etat["fenetres"].append(vus_depuis)
        return {i for i in ids if i in etat["soignes"]}
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous", _soignes)
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

    # L'équipe de la plateforme : son courriel est rangé à part, pour qu'on voie qu'il
    # n'est jamais celui d'un client.
    monkeypatch.setenv("OTO_ALERTES_EQUIPE_DESTINATAIRES", _EQUIPE)
    monkeypatch.setattr(org_store, "get_org", lambda o: {"id": o, "name": f"org {o}"})

    def _envoyer(m, to, subject, html):
        etat["equipe" if to == _EQUIPE else "lettres"].append((to, subject, html))
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
    assert bilan == {"orgs": 3, "envoyes": 1, "lettres": 1, "marques": 2,
                     "equipe": 1, "equipe_rates": 0,
                     "tenant_tiers_ecartes": 1, "sans_destinataire": 1}


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


def test_une_panne_de_la_plateforme_ne_s_annonce_pas_au_titulaire(bureau):
    # L'agent 2 a un épisode côté plateforme (`medecin_agents`) : son titulaire n'y peut
    # rien, il n'est ni nommé ni marqué ; l'agent 1, de la même org, part seul.
    bureau["soignes"] = {2}
    M.balayer(_JOUR)
    html = bureau["lettres"][0][2]
    assert "agent 1" in html and "agent 2" not in html
    assert 2 not in bureau["marques"]
    # Seulement tant que la boucle tient l'épisode : la fenêtre est celle du médecin.
    from oto_mcp import medecin_agents
    assert bureau["fenetres"] == [medecin_agents.FRAICHEUR]


def test_des_episodes_illisibles_laissent_partir_le_courriel(bureau, monkeypatch):
    from oto_mcp.db import episodes_agents

    def _casse(ids, *, vus_depuis):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous", _casse)
    M.balayer(_JOUR)
    assert "agent 2" in bureau["lettres"][0][2]


# ── Qui est prévenu : le CLIENT, pas l'opérateur qui a créé l'org ─────────────────────
# L'alerte partait au premier admin joignable par date d'arrivée — souvent l'opérateur
# de la plateforme qui avait monté l'org : les admins clients n'apprenaient rien.

def _org_avec_operateur(monkeypatch, *, roles, membres=None, suspendus=()):
    from oto_mcp import org_store
    from oto_mcp.db import users as U
    membres = membres or [
        {"sub": "op", "org_role": "org_admin"},     # arrivé le premier : l'opérateur
        {"sub": "a1", "org_role": "org_admin"},
        {"sub": "m1", "org_role": "org_member"},
        {"sub": "a2", "org_role": "org_admin"},
    ]
    monkeypatch.setattr(org_store, "list_org_members", lambda o: list(membres))
    monkeypatch.setattr(U, "get_user", lambda s: {
        "locale": "en" if s == "a2" else "fr", "role": roles.get(s, "member"),
        "suspended_at": "2026-11-01" if s in suspendus else None})


def test_tous_les_admins_clients_et_jamais_l_operateur(monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    assert M.destinataires(7) == [("a1", "a1@acme.test", True), ("a2", "a2@acme.test", True)]
    assert M.destinataire_admin(7) == ("a1", "a1@acme.test"), (
        "le premier destinataire est un admin CLIENT, plus l'opérateur arrivé avant lui")


def test_le_titulaire_de_l_agent_d_abord_meme_simple_membre(monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    _org_avec_operateur(monkeypatch, roles={"op": "admin"})
    assert M.destinataires(7, proprietaires=["m1", "a2", "parti", None]) == [
        ("m1", "m1@acme.test", False), ("a2", "a2@acme.test", True),
        ("a1", "a1@acme.test", True)], (
        "le titulaire est celui qui peut reconnecter ; un ancien membre n'est pas écrit")


def test_un_compte_suspendu_ou_sans_adresse_est_saute(monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "emails_by_subs",
                        lambda subs: {s: f"{s}@acme.test" for s in subs if s != "a2"})
    _org_avec_operateur(monkeypatch, roles={"op": "admin"})
    assert M.destinataires(7) == [("a1", "a1@acme.test", True)]
    _org_avec_operateur(monkeypatch, roles={"op": "admin"}, suspendus={"a1"})
    assert M.destinataires(7) == [("op", "op@acme.test", True)], (
        "aucun client joignable : l'opérateur, plutôt que personne")


def test_une_org_d_operateurs_seulement_garde_son_premier_admin(monkeypatch):
    # Une org interne ou de démonstration : le message part quelque part.
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin", "op2": "admin"},
                        membres=[{"sub": "op", "org_role": "org_admin"},
                                 {"sub": "op2", "org_role": "org_admin"}])
    assert M.destinataires(7) == [("op", "op@acme.test", True)]


def test_le_balayage_ecrit_a_chaque_client_dans_sa_langue(bureau, monkeypatch):
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    bureau["agents"] = [{**_agent(1, 7), "proprietaire": "m1"}]
    bilan = M.balayer(_JOUR)
    assert [(to, s) for to, s, _ in bureau["lettres"]] == [
        ("m1@acme.test", "Un agent programmé est bloqué"),
        ("a1@acme.test", "Un agent programmé est bloqué"),
        ("a2@acme.test", "A scheduled agent is blocked")]
    assert bilan["envoyes"] == 1 and bilan["lettres"] == 3 and bureau["marques"] == [1]


def test_un_seul_envoi_reussi_suffit_a_marquer(bureau, monkeypatch):
    from oto_mcp import email_templates
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})

    def _envoyer(m, to, subject, html):
        bureau["lettres"].append((to, subject, html))
        return to.startswith("a2")
    monkeypatch.setattr(email_templates, "_envoyer", _envoyer)
    bureau["agents"] = [_agent(1, 7)]
    bilan = M.balayer(_JOUR)
    assert bilan["lettres"] == 1 and bureau["marques"] == [1], (
        "rejouer pour celui qu'un envoi a raté renverrait le message à tous les autres")


# ── Ce que chacun reçoit : un admin tout, un membre ce qu'il a le droit de voir ───────

def _acces(monkeypatch, *, admins=("a1", "a2"), partages=None):
    """La règle d'accès des agents (`_acces_agent.niveaux`), jouée vraie sur des
    doublures de ses lectures : le rôle d'admin et les partages."""
    from oto_mcp import db, ownership, roles

    class _Portee:
        def __init__(self, sub):
            self.sub = sub

        def principal_pairs(self):
            return [("user", self.sub)]
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub in admins)
    monkeypatch.setattr(ownership, "accessor_scope", _Portee)
    lus = partages or {}
    monkeypatch.setattr(db, "partages_d_agents", lambda ids, princ: {
        i: lus[(princ[0][1], i)] for i in ids if (princ[0][1], i) in lus})


def test_un_membre_ne_recoit_que_ses_agents_et_un_admin_tout(bureau, monkeypatch):
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    # m1 tient l'agent 1 ; l'agent 2 est à a1 ; l'agent 3, à un autre membre, est
    # partagé en lecture à m1 ; l'agent 4, à ce même membre, ne l'est pas.
    _acces(monkeypatch, partages={("m1", 3): "read"})
    bureau["agents"] = [{**_agent(1, 7), "proprietaire": "m1"},
                        {**_agent(2, 7), "proprietaire": "a1"},
                        {**_agent(3, 7), "proprietaire": "x"},
                        {**_agent(4, 7), "proprietaire": "x"}]
    M.balayer(_JOUR)
    par_qui = {to: html for to, _s, html in bureau["lettres"]}
    assert set(par_qui) == {"m1@acme.test", "a1@acme.test", "a2@acme.test"}
    assert "agent 1" in par_qui["m1@acme.test"] and "agent 3" in par_qui["m1@acme.test"]
    assert "agent 2" not in par_qui["m1@acme.test"]
    assert "agent 4" not in par_qui["m1@acme.test"], "ni le sien ni partagé"
    for admin in ("a1@acme.test", "a2@acme.test"):
        assert all(f"agent {i}" in par_qui[admin] for i in (1, 2, 3, 4))
    assert sorted(bureau["marques"]) == [1, 2, 3, 4]


def test_un_membre_sans_rien_a_voir_ne_recoit_rien(bureau, monkeypatch):
    # Titulaire nommé, mais la règle d'accès ne lui rend aucun agent : rien ne lui part.
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "list_org_members", lambda o: [
        {"sub": "m1", "org_role": "org_member"}, {"sub": "a1", "org_role": "org_admin"}])
    _acces(monkeypatch, admins=("a1",))
    monkeypatch.setattr(M, "agents_visibles", lambda sub, org, agents: [])
    bureau["agents"] = [{**_agent(2, 7), "proprietaire": "m1"}]
    M.balayer(_JOUR)
    assert [to for to, _s, _h in bureau["lettres"]] == ["a1@acme.test"]


def test_l_equipe_recoit_un_courriel_separe_jamais_une_copie_du_client(bureau, monkeypatch):
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    _acces(monkeypatch)
    bureau["agents"] = [{**_agent(1, 7), "proprietaire": "m1"},
                        {**_agent(2, 7), "proprietaire": "a1"}]
    bilan = M.balayer(_JOUR)
    (to, subject, html), = bureau["equipe"]
    assert to == _EQUIPE and subject.startswith("[équipe] ")
    assert "org 7" in html and "(#7)" in html, "l'équipe sait de quelle org il s'agit"
    assert "agent 1" in html and "agent 2" in html, "le contenu de l'org, entier"
    assert "3 courriel(s) envoyé(s) dans l'org" in html
    for to_c, subject_c, html_c in bureau["lettres"]:
        assert to_c != _EQUIPE and _EQUIPE not in html_c
        assert "[équipe]" not in subject_c and "Copie pour l'équipe" not in html_c
    assert bilan["equipe"] == 1 and bilan["equipe_rates"] == 0


def test_plusieurs_adresses_d_equipe_chacune_son_courriel(bureau, monkeypatch):
    from oto_mcp import email_templates
    vus = []
    monkeypatch.setenv("OTO_ALERTES_EQUIPE_DESTINATAIRES",
                       " e1@exemple.invalid ,e2@exemple.invalid")
    monkeypatch.setattr(email_templates, "_envoyer",
                        lambda m, to, subject, html: vus.append(to) or True)
    bureau["agents"] = [_agent(1, 7)]
    assert M.balayer(_JOUR)["equipe"] == 2
    assert vus == ["a@acme.test", "e1@exemple.invalid", "e2@exemple.invalid"]


def test_un_courriel_d_equipe_rate_est_compte_et_l_episode_reste_marque(bureau, monkeypatch):
    from oto_mcp import email_templates

    def _envoyer(m, to, subject, html):
        return to != _EQUIPE
    monkeypatch.setattr(email_templates, "_envoyer", _envoyer)
    bureau["agents"] = [_agent(1, 7)]
    bilan = M.balayer(_JOUR)
    assert bilan["envoyes"] == 1 and bilan["equipe_rates"] == 1
    assert bureau["marques"] == [1], "le client l'a reçu : l'épisode est annoncé"


def test_un_envoi_client_rate_n_ecrit_pas_a_l_equipe(bureau):
    bureau["envoi_ok"] = False
    M.balayer(_JOUR)
    assert bureau["equipe"] == [] and bureau["marques"] == []


def test_sans_reglage_d_equipe_le_passage_leve_avant_tout_envoi(bureau, monkeypatch):
    from oto_mcp import alertes_equipe
    monkeypatch.delenv("OTO_ALERTES_EQUIPE_DESTINATAIRES")
    with pytest.raises(alertes_equipe.AlertesEquipeNonReglees):
        M.balayer(_JOUR)
    assert bureau["lettres"] == [] and bureau["marques"] == []

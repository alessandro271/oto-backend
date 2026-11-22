"""Prévenir l'admin quand un agent programmé est bloqué (signal 1168) — sans base.

Les propriétés qui font que ce passage répare quelque chose : un courriel PAR ORG, sous
la marque de l'org ; la marque posée APRÈS l'envoi, et seulement s'il est parti ; rien
pour une org d'un tenant tiers (comptée) ; rien aux heures calmes ; rien hors production
(la boucle se déclare `tiers=True`). Sous le réglage des alertes d'agents, le courriel
parle comme Slack : un bloc par problème, le texte du client, jamais le motif brut."""
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
    monkeypatch.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", raising=False)
    etat["soignes"] = set()
    etat["fenetres"] = []

    def _soignes(ids, *, vus_depuis):
        etat["fenetres"].append(vus_depuis)
        return {i for i in ids if i in etat["soignes"]}
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous", _soignes)
    monkeypatch.setattr(alertes_agents, "agents_bloques", lambda seuil: list(etat["agents"]))
    etat["attente"] = []
    monkeypatch.setattr(alertes_agents, "agents_sans_forfait", lambda: list(etat["attente"]))
    monkeypatch.setattr(alertes_agents, "marquer_annonces",
                        lambda ids: etat["marques"].extend(ids) or len(ids))
    etat["marques_forfait"] = []
    monkeypatch.setattr(alertes_agents, "marquer_forfait_annonces",
                        lambda ids: etat["marques_forfait"].extend(ids) or len(ids))
    # L'org 9 est celle d'un tenant tiers.
    monkeypatch.setattr(db, "org_tenant_slug",
                        lambda o: "partenaire" if int(o) == 9 else tenancy.primary_slug())
    # L'org 11 n'a aucun admin joignable.
    monkeypatch.setattr(org_store, "list_org_members", lambda o: [] if o == 11 else [
        {"sub": "m", "org_role": "member"}, {"sub": "a", "org_role": "org_admin"}])
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "fr"})
    monkeypatch.setattr(org_store, "org_front", lambda o: (None, None))

    # L'équipe de la plateforme = ses admins de plateforme (`alertes_equipe`) : son
    # courriel est rangé à part, pour qu'on voie qu'il n'est jamais celui d'un client.
    etat["operateurs"] = [{"sub": "op", "email": _EQUIPE, "role": "admin"}]
    monkeypatch.setattr(U, "operateurs_de_plateforme",
                        lambda admin_sub=None: list(etat["operateurs"]))
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


def test_plusieurs_admins_de_plateforme_chacun_son_courriel(bureau, monkeypatch):
    from oto_mcp import email_templates
    vus = []
    bureau["operateurs"] = [{"sub": "op1", "email": "e1@exemple.invalid", "role": "admin"},
                            {"sub": "op2", "email": "e2@exemple.invalid",
                             "role": "super_admin"}]
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


def test_sans_admin_de_plateforme_une_erreur_et_les_clients_recoivent(bureau, caplog):
    """Aucun compte n'a le rôle : une ligne ERROR qui nomme l'org, aucun courriel
    d'équipe, et le client reçoit le sien — ni repli, ni refus."""
    bureau["operateurs"] = []
    bureau["agents"] = [_agent(1, 7)]
    with caplog.at_level("ERROR", logger="oto_mcp.alertes_equipe"):
        bilan = M.balayer(_JOUR)
    assert bureau["equipe"] == []
    assert [to for to, _s, _h in bureau["lettres"]] == ["a@acme.test"]
    assert bilan["envoyes"] == 1 and bilan["equipe"] == 0
    assert bureau["marques"] == [1], "le client l'a reçu : l'épisode est annoncé"
    erreurs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(erreurs) == 1
    assert "org 7" in erreurs[0].getMessage()
    assert "aucun admin de plateforme" in erreurs[0].getMessage()


# ── Les agents qui ATTENDENT un abonnement à reconnecter ──────────────────────────────

def _attente(tid, org, statut="needs_login", proprietaire="a"):
    return {"trigger_id": tid, "org_id": org, "proprietaire": proprietaire,
            "libelle": f"agent {tid}", "famille": "claude_subscription",
            "forfait": statut, "travaux": 4, "depuis": "2026-11-08 18:31:00"}


def test_un_agent_qui_attend_son_forfait_s_annonce_et_se_marque_a_part(bureau):
    bureau["agents"] = []
    bureau["attente"] = [_attente(5, 7)]
    bilan = M.balayer(_JOUR)
    (to, subject, html), = bureau["lettres"]
    assert to == "a@acme.test" and subject == "Un agent programmé est bloqué"
    assert "agent 5" in html and "ne tourne plus" in html and "4 passage(s)" in html
    assert "à reconnecter : abonnement Claude" in html and "dans Connecteurs" in html
    assert "échouent passage après passage" not in html, "il n'échoue pas : il attend"
    assert bureau["marques"] == [] and bureau["marques_forfait"] == [5], (
        "l'attente a sa marque : l'épisode d'échecs reste annonçable")
    assert bilan["envoyes"] == 1 and bilan["marques"] == 1


def test_en_anglais_le_geste_et_l_abonnement_sont_nommes(bureau, monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "en"})
    bureau["agents"], bureau["attente"] = [], [_attente(5, 7)]
    M.balayer(_JOUR)
    html = bureau["lettres"][0][2]
    assert "has not run since" in html and "to reconnect: Claude plan" in html
    assert "in Connectors" in html


def test_chaque_famille_d_abonnement_a_son_libelle():
    from oto_mcp import runner_models
    assert set(runner_models.LIBELLES_ABONNEMENT) == set(runner_models.FAMILLES_PERSONNELLES)
    for libelles in runner_models.LIBELLES_ABONNEMENT.values():
        assert libelles["fr"] and libelles["en"]


def test_un_message_par_org_les_deux_pannes_ensemble_chaque_agent_une_fois(bureau):
    # L'agent 1 meurt ET attend (deux lignes du relevé) : nommé une fois, ses deux
    # épisodes marqués.
    bureau["attente"] = [_attente(1, 7), _attente(5, 7), _attente(5, 7)]
    M.balayer(_JOUR)
    assert len(bureau["lettres"]) == 1
    html = bureau["lettres"][0][2]
    assert html.count("agent 1") == 1 and html.count("agent 5") == 1
    assert "passages de suite en échec" in html and "ne tourne plus" in html
    assert sorted(bureau["marques"]) == [1, 2]
    assert sorted(set(bureau["marques_forfait"])) == [1, 5]


def test_un_membre_ne_voit_que_ses_agents_en_attente(bureau, monkeypatch):
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    _acces(monkeypatch)
    bureau["agents"] = []
    bureau["attente"] = [_attente(5, 7, proprietaire="m1"), _attente(6, 7, proprietaire="a1")]
    M.balayer(_JOUR)
    par_qui = {to: html for to, _s, html in bureau["lettres"]}
    assert "agent 5" in par_qui["m1@acme.test"] and "agent 6" not in par_qui["m1@acme.test"]
    assert "agent 5" in par_qui["a1@acme.test"] and "agent 6" in par_qui["a1@acme.test"]
    (_to, _s, equipe), = bureau["equipe"]
    assert "agent 5" in equipe and "agent 6" in equipe


def test_une_lecture_ratee_des_attentes_laisse_partir_les_echecs(bureau, monkeypatch):
    from oto_mcp.db import alertes_agents

    def _casse():
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(alertes_agents, "agents_sans_forfait", _casse)
    assert M.balayer(_JOUR)["envoyes"] == 1


# ── Sous le réglage des alertes : le courriel parle comme Slack ─────────────────

_COPIE = {"fr": {"headline": "La clé de l'outil d'emailing a été refusée.",
                 "todo": "Recréez-la et collez-la dans Connecteurs.",
                 "action": "connectors"}}


def _episode(tid, **kw):
    return {"id": 100 + tid, "org_id": 7, "trigger_id": tid, "classe": "cle_refusee",
            "cote": "proprietaire", "statut": "ouvert", "motif": "HTTP 403 <Access denied>",
            "message_client": None, "cause_copie": None, "cause_cote": None, **kw}


@pytest.fixture()
def comme_slack(bureau, monkeypatch):
    from oto_mcp import links
    from oto_mcp.db import sante_agents
    monkeypatch.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "1")
    monkeypatch.setattr(M, "_annonces_sur_slack", lambda ids: set())
    bureau["vivants"] = {7: []}

    def _vivants(org):
        if bureau["vivants"] is None:
            raise RuntimeError("base indisponible")
        return list(bureau["vivants"].get(org, []))
    monkeypatch.setattr(sante_agents, "episodes_vivants", _vivants)
    base = "https://app.acme.test"
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p: (
        base if kind == "home" else f"{base}/{kind}/{p.get('id', '')}".rstrip("/")))
    return bureau


def test_les_agents_d_un_meme_probleme_forment_un_bloc_sans_le_motif(comme_slack):
    comme_slack["vivants"][7] = [
        _episode(1, cause_copie=_COPIE, cause_cote="proprietaire"),
        _episode(2, cause_copie=_COPIE, cause_cote="proprietaire")]
    M.balayer(_JOUR)
    (to, sujet, html), = comme_slack["lettres"]
    assert (to, sujet) == ("a@acme.test", "2 agents sont arrêtés")
    assert "Access denied" not in html and "HTTP 403" not in html, "jamais le motif brut"
    # Le preheader (caché, la ligne d'aperçu) reprend le premier problème : le corps,
    # lui, ne le dit qu'une fois.
    corps = html.split("</div>", 1)[1]
    assert corps.count("La clé de l'outil") == 1, "un problème = un bloc, le texte une fois"
    assert "agent 1" in html and "agent 2" in html
    assert 'href="https://app.acme.test/agent/1"' in html, "chaque agent mène à sa page"
    assert "https://app.acme.test/connectors" in html, "le bouton du geste"
    assert comme_slack["marques"] == [1, 2], "marqués après l'envoi, comme avant"


def test_deux_problemes_deux_blocs_et_le_lien_du_medecin_hors_produit_est_omis(comme_slack):
    comme_slack["vivants"][7] = [
        _episode(1, cause_copie=_COPIE, cause_cote="proprietaire"),
        _episode(2, classe="inconnu", message_client={
            "headline": "Le compte de l'agent n'a plus accès au tableur.",
            "todo": "Partagez de nouveau le tableur avec lui.",
            "link": "https://ailleurs.test/piege"})]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Le compte de l" in html and "Partagez de nouveau" in html
    assert "La clé de l" in html
    assert "ailleurs.test" not in html, "un lien qui mène hors du produit est omis"
    assert "Access denied" not in html


def test_un_agent_sans_episode_lisible_recoit_la_phrase_par_defaut(comme_slack):
    comme_slack["vivants"] = None          # lecture des épisodes en échec : ouvert
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Il s'est arrêté avant la fin de son travail." in html
    assert "Access denied" not in html
    assert comme_slack["marques"] == [1, 2]


def test_le_texte_du_medecin_est_echappe(comme_slack):
    comme_slack["vivants"][7] = [_episode(1, message_client={
        "headline": "<script>x</script> refusé", "todo": None, "link": None})]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_en_anglais_le_courriel_par_probleme(comme_slack, monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "en"})
    M.balayer(_JOUR)
    sujet, html = comme_slack["lettres"][0][1:]
    assert sujet == "2 agents stopped"
    assert "It stopped before finishing its work." in html
    assert "Failing since" in html and "Oct 20, 06:00 UTC" in html


def test_une_panne_pas_encore_comprise_ne_dit_pas_que_c_est_au_client(comme_slack):
    """Côté `inconnu`, sans texte du médecin : la phrase de l'onglet Santé, jamais celle
    d'une classe qui dirait au client de reconnecter quoi que ce soit."""
    comme_slack["vivants"][7] = [_episode(1, classe="cle_refusee", cote="inconnu")]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Nous cherchons pourquoi il s" in html
    assert "Reconnectez" not in html and "https://app.acme.test/connectors" not in html


def test_le_courriel_par_probleme_dit_qu_il_part_a_l_admin(comme_slack, monkeypatch):
    M.balayer(_JOUR)
    assert "en tant qu'administrateur de cet espace" in comme_slack["lettres"][0][2]
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "en"})
    comme_slack["lettres"].clear()
    comme_slack["marques"].clear()
    M.balayer(_JOUR)
    assert "as an administrator of this workspace" in comme_slack["lettres"][0][2]


def test_la_phrase_d_une_panne_pas_comprise_est_celle_de_l_onglet_sante(comme_slack):
    from oto_mcp.capabilities.agent_health import A_NOUS_INCONNU
    pb = M.probleme_de_l_agent(7, {}, _episode(1, cote="inconnu"), "fr", marque="Acme",
                               sub="a")
    assert pb == {"type": "echec", "headline": A_NOUS_INCONNU["fr"], "todo": None,
                  "action": None, "link": None}


def test_un_type_de_probleme_sans_phrase_leve_au_lieu_de_dire_un_echec(comme_slack,
                                                                      monkeypatch):
    """Un agent sans épisode reçoit la phrase de SON type ; un type qui n'en a pas ne
    retombe pas sur celle d'un échec."""
    monkeypatch.setattr(M, "type_de_probleme", lambda agent: "autre")
    with pytest.raises(ValueError):
        M.probleme_de_l_agent(7, {}, None, "fr", marque="Acme", sub="a")


def test_chaque_destinataire_recoit_ce_qu_il_voit(comme_slack, monkeypatch):
    """`destinataires_de_l_alerte` décide qui reçoit et ce que chacun voit : un courriel
    par destinataire, sur SES agents ; rien à voir, rien ne part. L'org se marque dès
    qu'un client l'a reçu."""
    agents = {int(a["trigger_id"]): a for a in comme_slack["agents"]}
    monkeypatch.setattr(M, "destinataires_de_l_alerte", lambda org, ags, equipe=(): [
        {"sub": "a", "adresse": "a@acme.test", "agents": [agents[1]], "qualite": "admin"},
        {"sub": "b", "adresse": "b@acme.test", "agents": [], "qualite": "admin"}]
        if org == 7 else [])
    bilan = M.balayer(_JOUR)
    assert [l[0] for l in comme_slack["lettres"]] == ["a@acme.test"]
    assert "agent 2" not in comme_slack["lettres"][0][2]
    assert comme_slack["marques"] == [1, 2]
    assert bilan["envoyes"] == 1


def test_le_courriel_par_probleme_dit_depuis_quand(comme_slack):
    comme_slack["vivants"][7] = [_episode(1), _episode(2)]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "En échec depuis" in html and "20 oct., 06:00 UTC" in html


def test_un_seul_agent_est_nomme_dans_le_sujet(comme_slack, bureau):
    bureau["agents"] = bureau["agents"][:1]
    comme_slack["vivants"][7] = [_episode(1)]
    M.balayer(_JOUR)
    sujet = comme_slack["lettres"][0][1]
    assert sujet.endswith(" est arrêté") and "1" in sujet


def test_le_connecteur_et_la_reponse_du_fournisseur_sont_dits_jamais_notre_texte(
        comme_slack, bureau):
    """Le connecteur que le motif nomme, sous son libellé, et ce que le fournisseur a
    répondu ; un motif interne ne sort pas."""
    bureau["agents"][0]["motif"] = ("BadRequestError : HTTP 400 invalid_request_error — "
                                    "Your credit balance is too low to access the "
                                    "Anthropic API.")
    bureau["agents"][1]["motif"] = "RuntimeError : run_start : `x_y` refusé, voir oto_list"
    comme_slack["vivants"][7] = [
        _episode(1, cause_copie=_COPIE, cause_cote="proprietaire",
                 motif="pas de clé `anthropic` déposée"),
        _episode(2, classe="inconnu", message_client={"headline": "Autre chose.", "todo": None})]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "HTTP 400 : Your credit balance is too low" in html, "la réponse du fournisseur"
    assert ">Connecteur<" in html and ">Anthropic<" in html, "le connecteur, sous son libellé"
    assert "RuntimeError" not in html and "oto_list" not in html, "jamais notre texte"
    assert "passages en échec" in html


def test_par_probleme_un_membre_un_admin_et_l_equipe_lisent_chacun_leur_titre(
        comme_slack, monkeypatch):
    """La ligne de fin dit à quel titre on reçoit : « administrateur » pour un admin
    seulement ; un membre, par son accès aux agents ; l'équipe, sa copie séparée."""
    _org_avec_operateur(monkeypatch, roles={"op": "super_admin"})
    _acces(monkeypatch)
    comme_slack["agents"] = [{**_agent(1, 7), "proprietaire": "m1"}]
    M.balayer(_JOUR)
    par_qui = {to: html for to, _s, html in comme_slack["lettres"]}
    assert "en tant qu'administrateur" not in par_qui["m1@acme.test"]
    assert "vous avez accès à ces agents" in par_qui["m1@acme.test"]
    assert "en tant qu'administrateur" in par_qui["a1@acme.test"]
    (_to, sujet, equipe), = comme_slack["equipe"]
    assert sujet.startswith("[équipe] ") and "(#7)" in equipe
    assert "Copie pour l'équipe de la plateforme" in equipe
    assert "en tant qu'administrateur" not in equipe


def test_par_probleme_l_attente_d_abonnement_est_un_probleme_a_part(comme_slack):
    """Un agent qui attend un abonnement : son type (`forfait`), sa phrase, le geste de
    reconnexion, jamais la phrase d'un échec ; sa marque propre."""
    comme_slack["agents"] = [_agent(1, 7)]
    comme_slack["attente"] = [_attente(5, 7)]
    M.balayer(_JOUR)
    (_to, _s, html), = comme_slack["lettres"]
    assert "Il attend que son abonnement Claude soit reconnecté." in html
    assert "reconnecte dans Connecteurs" in html
    assert "Ne tourne plus depuis" in html and "En échec depuis" in html, \
        "deux problèmes, deux blocs"
    assert "https://app.acme.test/connectors" in html
    assert comme_slack["marques"] == [1] and comme_slack["marques_forfait"] == [5]

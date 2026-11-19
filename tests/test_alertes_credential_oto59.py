"""L'alerte hors bande : prévenir par un canal qui ne meurt pas avec la clé (oto#59).

Le 03/09/2026, une clé a disparu d'une org. Une douzaine de passages programmés ont
tourné à l'aveugle pendant **36 heures**. ⚠️ Le canal qui aurait annoncé la panne
tournait sur le credential tombé : six fois par jour, un run découvrait qu'il était
cassé, l'inscrivait sur une ligne que personne ne regardait, et se taisait —
**correctement, selon ses propres règles**. La panne était silencieuse *par
construction*.

Ces bancs tiennent les quatre propriétés qui font que ce lot répare quelque chose : le
courrier passe par la PLATEFORME, il y en a **un par org**, il ne part pas sans raison,
et il ne part pas du tout tant que l'interrupteur est fermé.
"""
from __future__ import annotations

import pytest

from oto_mcp import maintenance as M


@pytest.fixture()
def bureau(monkeypatch):
    """Une org à prévenir, un admin joignable, et un facteur qui compte ses lettres."""
    envois: list = []
    from oto_mcp.db import alertes_credential as A
    from oto_mcp.db import users as U
    from oto_mcp import email as E
    from oto_mcp import org_store as O

    monkeypatch.setattr(A, "a_notifier", lambda: [
        {"org_id": 7, "ids": [1, 2], "connectors": ["slack"], "agents_max": 3,
         "depuis": None},
    ])
    marques: list = []
    monkeypatch.setattr(A, "marquer_notifie", lambda ids: marques.extend(ids) or len(ids))
    # Aucune clé rouge par défaut : ces bancs-ci tiennent le RETRAIT ; les clés
    # refusées ou à sec ont les leurs plus bas.
    monkeypatch.setattr(A, "clore_episodes_gueris", lambda: 0)
    monkeypatch.setattr(A, "cles_ko_a_annoncer", lambda: [])
    # Les agents qui dépendent encore d'un connecteur retiré : aucun connu par défaut,
    # l'alerte dit alors leur NOMBRE (`agents_max`).
    from oto_mcp.db import runner_triggers as T
    monkeypatch.setattr(T, "triggers_actifs_utilisant", lambda org, c: [])
    monkeypatch.setattr(O, "list_org_members",
                        lambda org: [{"sub": "u-admin", "org_role": "org_admin"},
                                     {"sub": "u-membre", "org_role": "org_member"}])
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {"u-admin": "a@exemple.invalid"})
    # Une org de la plateforme (`front_brand` NULL ⟹ la marque de l'instance), un admin
    # sans préférence de langue.
    monkeypatch.setattr(O, "org_front", lambda org: (None, None))
    monkeypatch.setattr(U, "get_user", lambda sub: {"sub": sub, "locale": None})
    # `_envoyer` passe `to, subject, html` en POSITION, comme `_send` le déclare. Le
    # courriel d'ÉQUIPE (`alertes_equipe`) est rangé à part, dans `EQUIPE_VUS`.
    monkeypatch.setenv("OTO_ALERTES_EQUIPE_DESTINATAIRES", EQUIPE)
    monkeypatch.setattr(O, "get_org", lambda org: {"id": org, "name": f"org {org}"})
    EQUIPE_VUS.clear()
    monkeypatch.setattr(E, "_send", lambda to, subject, html, **kw: (
        EQUIPE_VUS if to == EQUIPE else envois).append(
        {"to": to, "subject": subject, "html": html, **kw}) or True)
    return envois, marques


EQUIPE = "equipe@exemple.invalid"
EQUIPE_VUS: list = []


def test_ferme_par_defaut_rien_ne_part(bureau, monkeypatch):
    """⚠️ LE banc du dispositif : le mécanisme tourne, l'effet attend une décision.
    Un canal qu'on ouvre en devinant son volume se referme au bout d'une semaine."""
    envois, marques = bureau
    monkeypatch.delenv(M._ENV_ALERTE, raising=False)
    out = M.alertes_credential()
    assert envois == [] and marques == []
    assert out["actif"] is False
    assert out["orgs_a_prevenir"] == 1, (
        "le travail doit DIRE ce qui partirait — rendre zéro se lirait « rien à "
        "signaler », ce qui est le contraire de la vérité")
    assert out["note"], "le silence doit s'expliquer, pas se deviner"


def test_ouvert_un_seul_courriel_par_ORG(bureau, monkeypatch):
    """Trois clés retirées le même jour font UN message. Trois messages pour un
    incident apprennent au destinataire à les ignorer."""
    envois, marques = bureau
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert len(envois) == 1 and out["envoyes"] == 1
    assert envois[0]["to"] == "a@exemple.invalid"
    assert marques == [1, 2], "les deux lignes groupées sont marquées, pas une seule"


def test_le_courriel_part_par_le_courrier_de_PLATEFORME(bureau, monkeypatch):
    """⚠️ La propriété qui distingue cette alerte du registre qu'elle remplace : le
    canal qui prévient ne doit pas pouvoir mourir avec ce dont il annonce la mort. On
    exige donc l'envoi par le mailer de plateforme, jamais par un connecteur de l'org."""
    envois, _ = bureau
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    M.alertes_credential()
    assert "html" in envois[0] and "subject" in envois[0], (
        "l'envoi doit passer par `email._send` (mailer de plateforme) — un connecteur "
        "d'org n'a pas cette signature, et il serait mort avec la clé")


def test_rien_a_prevenir_rien_ne_part(bureau, monkeypatch):
    """Un avertissement qu'on reçoit toujours cesse d'être lu."""
    envois, _ = bureau
    from oto_mcp.db import alertes_credential as A
    monkeypatch.setattr(A, "a_notifier", lambda: [])
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert envois == [] and out["orgs_a_prevenir"] == 0 and out["envoyes"] == 0


def test_sans_destinataire_la_ligne_reste_A_NOTIFIER(bureau, monkeypatch):
    """⚠️ On ne marque PAS : le jour où l'org gagne un admin, l'alerte partira. La
    perdre ici serait la perdre exactement quand elle devient délivrable."""
    envois, marques = bureau
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {})
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert envois == [] and marques == []
    assert out["sans_destinataire"] == 1, "et ça se compte, sinon ça ne se voit pas"


def test_dry_run_compte_sans_envoyer(bureau, monkeypatch):
    envois, marques = bureau
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential(dry_run=True)
    assert envois == [] and marques == [] and out["orgs_a_prevenir"] == 1


def test_le_travail_tourne_dans_le_timer_quotidien():
    """Le mécanisme doit tourner dès le tag — sinon « l'effet attend une décision »
    voudrait dire « rien n'est prêt le jour de la décision »."""
    assert "alertes-credential" in M._TRAVAUX
    assert "alertes-credential" in M._ALL
    assert "alertes-credential" not in M._ACTES, (
        "ce n'est pas un acte d'opérateur : il tourne seul, gardé par son interrupteur")


# --- Clés REFUSÉES ou À SEC sous des agents programmés (signaux oto #1168, #1189) ------


def _rouge(connector="snitcher", org_id=7, verdict=None, raison="403 Forbidden"):
    return {"entity_type": "org", "entity_id": str(org_id), "connector": connector,
            "account": "", "raison": raison, "verdict": verdict, "org_id": org_id}


@pytest.fixture()
def cles_rouges(bureau, monkeypatch):
    """Une clé refusée dans l'org 7, un agent programmé qui en dépend, aucun retrait."""
    envois, _ = bureau
    from oto_mcp.db import alertes_credential as A
    from oto_mcp.db import runner_triggers as T
    lignes = [_rouge()]
    annoncees: list = []
    monkeypatch.setattr(A, "a_notifier", lambda: [])
    monkeypatch.setattr(A, "cles_ko_a_annoncer", lambda: lignes)
    monkeypatch.setattr(A, "marquer_cles_annoncees",
                        lambda ls: annoncees.extend(ls) or len(ls))
    agents = {("7", "snitcher"): [{"label": "veille quotidienne"}]}
    monkeypatch.setattr(T, "triggers_actifs_utilisant",
                        lambda org, c: agents.get((str(org), c), []))
    return envois, lignes, annoncees, agents


def test_une_cle_REFUSEE_sous_un_agent_programme_est_annoncee(cles_rouges, monkeypatch):
    """33 passages d'une même org ont buté sur une clé morte sans que personne soit
    prévenu (#1168). La marque rouge existait ; personne ne la portait hors bande."""
    envois, _, annoncees, _ = cles_rouges
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert out["envoyes"] == 1 and out["cles_en_panne"] == 1
    assert "Clé Snitcher refusée" in envois[0]["html"], (
        "le connecteur par son NOM de produit, jamais le slug en minuscules")
    assert "veille quotidienne" in envois[0]["html"], "l'agent qui en dépend est NOMMÉ"
    assert "403 Forbidden" in envois[0]["html"], "ce que le fournisseur a répondu"
    assert "Créez une nouvelle clé chez Snitcher et collez-la dans Connecteurs." in (
        envois[0]["html"]), "refusée : le geste est de recréer la clé"
    assert [l["connector"] for l in annoncees] == ["snitcher"], (
        "la clé est marquée annoncée APRÈS l'envoi : c'est ce qui fait UNE alerte par "
        "épisode, pas une par passage")


def test_une_cle_A_SEC_dit_recharger_pas_reposer(cles_rouges, monkeypatch):
    envois, lignes, _, _ = cles_rouges
    from oto_mcp.credentials_store import NO_QUOTA_VERDICT
    lignes[0].update(verdict=NO_QUOTA_VERDICT, raison="Not enough credits")
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    M.alertes_credential()
    html = envois[0]["html"]
    assert "Compte Snitcher sans crédit" in html
    assert ("Rechargez votre compte Snitcher chez Snitcher, ou branchez une autre clé "
            "dans Connecteurs.") in html, "à sec : le geste est CHEZ le fournisseur"
    assert "Créez une nouvelle clé" not in html, "jamais « recréer » une clé qui marche"


def test_en_anglais_chaque_etat_dit_son_geste(cles_rouges, monkeypatch):
    envois, lignes, _, _ = cles_rouges
    from oto_mcp.credentials_store import NO_QUOTA_VERDICT
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "en"})
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    M.alertes_credential()
    assert "Create a new key with Snitcher and paste it in Connectors." in envois[0]["html"]
    envois.clear()
    lignes[0].update(verdict=NO_QUOTA_VERDICT)
    from oto_mcp.db import alertes_credential as A
    monkeypatch.setattr(A, "cles_ko_a_annoncer", lambda: list(lignes))
    M.alertes_credential()
    html = envois[0]["html"]
    assert ("Top up your Snitcher account with Snitcher, or connect another key in "
            "Connectors.") in html
    assert "These agents fail on every run until then." in html


def test_une_cle_rouge_que_RIEN_ne_fait_tourner_ne_part_pas(cles_rouges, monkeypatch):
    """Sans agent programmé, pas d'urgence hors bande : la carte rouge suffit. Et
    l'épisode n'est PAS marqué : le jour où un agent en dépend, l'alerte partira."""
    envois, _, annoncees, agents = cles_rouges
    agents.clear()
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert envois == [] and annoncees == [] and out["orgs_a_prevenir"] == 0


def test_retrait_et_cle_rouge_dans_la_meme_org_font_UN_courriel(cles_rouges, monkeypatch):
    envois, _, annoncees, _ = cles_rouges
    from oto_mcp.db import alertes_credential as A
    marques: list = []
    monkeypatch.setattr(A, "a_notifier", lambda: [
        {"org_id": 7, "ids": [4], "connectors": ["slack"], "agents_max": 1,
         "depuis": None}])
    monkeypatch.setattr(A, "marquer_notifie", lambda ids: marques.extend(ids) or len(ids))
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert len(envois) == 1 and out["orgs_a_prevenir"] == 1
    assert "Clé Slack retirée" in envois[0]["html"] and "Snitcher" in envois[0]["html"]
    assert "Reposez une clé dans Connecteurs." in envois[0]["html"], (
        "retirée : le geste est de reposer une clé")
    assert marques == [4] and len(annoncees) == 1


def test_ferme_par_defaut_la_cle_rouge_se_COMPTE_sans_partir(cles_rouges, monkeypatch):
    envois, _, annoncees, _ = cles_rouges
    monkeypatch.delenv(M._ENV_ALERTE, raising=False)
    out = M.alertes_credential()
    assert envois == [] and annoncees == []
    assert out["cles_en_panne"] == 1 and out["orgs_a_prevenir"] == 1 and out["note"]


# --- Langue de l'instance (08/10/2026) -----------------------------------------------


def test_une_instance_anglophone_ecrit_l_alerte_en_anglais(cles_rouges, bureau, monkeypatch):
    """Le texte était écrit en dur en français, quelle que soit l'instance."""
    envois, _, _, _ = cles_rouges
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    monkeypatch.setenv("OTO_BRAND_LANGUE", "en")
    M.alertes_credential()
    assert envois[0]["subject"] == "A key is failing under your scheduled agents"
    assert "Snitcher key refused by the provider" in envois[0]["html"]
    assert "refusée" not in envois[0]["html"]


def test_le_retrait_s_ecrit_aussi_en_anglais(bureau, monkeypatch):
    envois, _ = bureau
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    monkeypatch.setenv("OTO_BRAND_LANGUE", "en")
    M.alertes_credential()
    assert envois[0]["subject"] == "A key was removed under your scheduled agents"
    assert "3 active scheduled agent(s)" in envois[0]["html"]


# --- La marque de l'ORG, pas celle de l'instance (revue de #1182) ---------------------


def test_l_alerte_part_sous_la_marque_l_expediteur_et_la_langue_du_tenant_de_l_org(
        bureau, monkeypatch):
    """Ses invitations partaient sous la marque de son tenant, l'alerte sous celle de
    l'instance et depuis son expéditeur : la même org recevait deux produits."""
    envois, _ = bureau
    from oto_mcp import org_store as O
    from oto_mcp import tenancy
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "issuer": "https://auth.acme.test/oidc", "brand": {
            "nom": "Acme", "site": "acme.test", "expediteur": "Acme <noreply@acme.test>",
            "langue": "en", "fond": "#101014", "surface": "#18181d",
            "encre": "#f5f5f7", "discret": "#a0a0ab", "filet": "#2a2a31",
            "bouton_fond": "#f5f5f7", "bouton_encre": "#101014"}}])))
    try:
        monkeypatch.setattr(O, "org_front", lambda org: ("https://app.acme.test", "acme"))
        monkeypatch.setenv(M._ENV_ALERTE, "1")
        M.alertes_credential()
    finally:
        tenancy.install(avant)
    (e,) = envois
    assert e["from_email"] == "Acme <noreply@acme.test>"
    assert e["subject"] == "A key was removed under your scheduled agents"
    assert "Acme" in e["html"]


def test_la_preference_de_l_admin_prime_sur_la_langue_de_l_instance(bureau, monkeypatch):
    envois, _ = bureau
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda sub: {"sub": sub, "locale": "en"})
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    monkeypatch.setenv("OTO_BRAND_LANGUE", "")
    M.alertes_credential()
    assert envois[0]["subject"] == "A key was removed under your scheduled agents"


# --- Ce que dit l'alerte, clé par clé (15/11/2026) -------------------------------------


def test_l_alerte_nomme_chaque_agent_et_le_relie_a_sa_page(cles_rouges, monkeypatch):
    """« Une clé est refusée » sans dire quels agents en dépendent laisse le
    destinataire les chercher : chacun est nommé, et lien vers SA page quand
    l'instance déclare ce patron."""
    envois, _, _, agents = cles_rouges
    agents[("7", "snitcher")] = [{"id": 41, "label": "veille <quotidienne>"},
                                 {"id": 42, "label": "relance"}]
    from oto_mcp import links
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, org=None, id=None, **kw:
                        f"https://app.exemple.test/{kind}/{org}/{id}")
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    M.alertes_credential()
    html = envois[0]["html"]
    assert 'href="https://app.exemple.test/agent/7/41"' in html
    assert 'href="https://app.exemple.test/agent/7/42"' in html
    assert "veille &lt;quotidienne&gt;" in html, "un libellé d'agent est échappé"


def test_un_seul_bouton_vers_connecteurs_et_aucun_sans_patron(cles_rouges, monkeypatch):
    envois, _, _, _ = cles_rouges
    from oto_mcp import links
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, org=None, id=None, **kw:
                        f"https://app.exemple.test/{kind}/{org}" if kind == "connectors"
                        else None)
    M.alertes_credential()
    html = envois[0]["html"]
    assert html.count('href="https://app.exemple.test/connectors/7"') == 1
    assert "Ouvrir Connecteurs" in html
    envois.clear()
    monkeypatch.setattr(links, "link_for", lambda *a, **kw: None)
    from oto_mcp.db import alertes_credential as A
    monkeypatch.setattr(A, "cles_ko_a_annoncer", lambda: [_rouge()])
    M.alertes_credential()
    assert "Ouvrir Connecteurs" not in envois[0]["html"], (
        "sans patron déclaré, pas de bouton : on n'envoie pas quelque part au hasard")


def test_un_retrait_nomme_les_agents_qui_en_dependent_encore(bureau, monkeypatch):
    envois, _ = bureau
    from oto_mcp.db import runner_triggers as T
    monkeypatch.setattr(T, "triggers_actifs_utilisant", lambda org, c:
                        [{"id": 9, "label": "digest hebdo"}] if c == "slack" else [])
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    M.alertes_credential()
    html = envois[0]["html"]
    assert "Clé Slack retirée" in html and "digest hebdo" in html
    assert "3 agent(s)" not in html, "des agents nommés remplacent leur nombre"


def test_chaque_admin_client_est_prevenu_jamais_l_operateur(bureau, monkeypatch):
    """L'alerte de clé partait au premier admin par date d'arrivée — souvent l'opérateur
    qui avait monté l'org. Elle part désormais à chaque admin client ; un retrait relève
    de l'org : le titulaire d'un agent touché, simple membre, ne le reçoit pas."""
    envois, marques = bureau
    from oto_mcp import org_store as O
    from oto_mcp.db import runner_triggers as T
    from oto_mcp.db import users as U
    monkeypatch.setattr(O, "list_org_members", lambda org: [
        {"sub": "u-op", "org_role": "org_admin"},
        {"sub": "u-admin", "org_role": "org_admin"},
        {"sub": "u-membre", "org_role": "org_member"}])
    monkeypatch.setattr(U, "emails_by_subs",
                        lambda subs: {s: f"{s}@exemple.invalid" for s in subs})
    monkeypatch.setattr(U, "get_user", lambda sub: {
        "sub": sub, "locale": None, "role": "super_admin" if sub == "u-op" else "member"})
    monkeypatch.setattr(T, "triggers_actifs_utilisant", lambda org, c: [
        {"id": 5, "label": "veille", "sub": "u-membre", "enabled": True}])
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    out = M.alertes_credential()
    assert [e["to"] for e in envois] == ["u-admin@exemple.invalid"]
    assert out["envoyes"] == 1 and marques == [1, 2]


# ── Un membre ne reçoit que SA clé sous SES agents ; l'équipe, un courriel séparé ─────

@pytest.fixture()
def org_mixte(bureau, monkeypatch):
    """L'org 7 : un admin client, deux membres. En panne : une clé d'ORG (sous l'agent
    de m1 et celui de m2), la clé PERSONNELLE de m1 (sous son agent et sous celui de
    m2, qui tourne sur la clé de m2), et un retrait."""
    envois, marques = bureau
    from oto_mcp import org_store as O
    from oto_mcp.db import alertes_credential as A
    from oto_mcp.db import runner_triggers as T
    from oto_mcp.db import users as U
    monkeypatch.setattr(O, "list_org_members", lambda org: [
        {"sub": "adm", "org_role": "org_admin"},
        {"sub": "m1", "org_role": "org_member"},
        {"sub": "m2", "org_role": "org_member"}])
    monkeypatch.setattr(U, "emails_by_subs",
                        lambda subs: {s: f"{s}@exemple.invalid" for s in subs})
    lignes = [_rouge(connector="snitcher"),
              {**_rouge(connector="lemlist"), "entity_type": "member",
               "entity_id": "7:m1"}]
    annoncees: list = []
    monkeypatch.setattr(A, "cles_ko_a_annoncer", lambda: lignes)
    monkeypatch.setattr(A, "marquer_cles_annoncees",
                        lambda ls: annoncees.extend(ls) or len(ls))
    agents = {"snitcher": [{"id": 1, "label": "veille de m1", "sub": "m1"},
                           {"id": 2, "label": "veille de m2", "sub": "m2"}],
              "lemlist": [{"id": 3, "label": "relance de m1", "sub": "m1"},
                          {"id": 4, "label": "relance de m2", "sub": "m2"}],
              "slack": [{"id": 5, "label": "digest de m2", "sub": "m2"}]}
    monkeypatch.setattr(T, "triggers_actifs_utilisant", lambda org, c: agents.get(c, []))
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    return envois, marques, annoncees


def test_un_membre_ne_recoit_que_sa_cle_sous_ses_agents(org_mixte):
    envois, _marques, _annoncees = org_mixte
    M.alertes_credential()
    par_qui = {e["to"]: e["html"] for e in envois}
    assert set(par_qui) == {"m1@exemple.invalid", "adm@exemple.invalid"}, (
        "m2 n'a ni clé personnelle en panne ni droit sur ce qui relève de l'org")
    m1 = par_qui["m1@exemple.invalid"]
    assert "relance de m1" in m1
    assert "relance de m2" not in m1, "l'agent de m2 tourne sur la clé de m2"
    assert "veille de m1" not in m1 and "Snitcher" not in m1, "clé d'org : admins seuls"
    assert "Slack" not in m1, "un retrait relève de l'org : admins seuls"


def test_un_admin_recoit_tout(org_mixte):
    envois, marques, annoncees = org_mixte
    out = M.alertes_credential()
    adm = next(e["html"] for e in envois if e["to"] == "adm@exemple.invalid")
    for nom in ("veille de m1", "veille de m2", "relance de m1", "relance de m2",
                "digest de m2"):
        assert nom in adm
    assert out["envoyes"] == 1 and marques == [1, 2] and len(annoncees) == 2


def test_l_equipe_recoit_un_courriel_separe_par_org(org_mixte):
    envois, _marques, _annoncees = org_mixte
    out = M.alertes_credential()
    (e,) = EQUIPE_VUS
    assert e["subject"].startswith("[équipe] ")
    assert "org 7" in e["html"] and "(#7)" in e["html"]
    for nom in ("veille de m1", "relance de m2", "digest de m2"):
        assert nom in e["html"]
    assert all(c["to"] != EQUIPE and EQUIPE not in c["html"]
               and "Copie pour l'équipe" not in c["html"] for c in envois)
    assert out["equipe"] == 1 and out["equipe_rates"] == 0


def test_sans_reglage_d_equipe_l_envoi_ouvert_refuse_avant_tout_envoi(bureau, monkeypatch):
    from oto_mcp import alertes_equipe
    envois, marques = bureau
    monkeypatch.delenv("OTO_ALERTES_EQUIPE_DESTINATAIRES")
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    with pytest.raises(alertes_equipe.AlertesEquipeNonReglees):
        M.alertes_credential()
    assert envois == [] and marques == []
    # Le travail refuse, et la maintenance sort en 1 même sans `--strict`.
    assert M.run(["alertes-credential"]) == 1


def test_sans_reglage_d_equipe_un_passage_qui_n_envoie_pas_passe(bureau, monkeypatch):
    monkeypatch.delenv("OTO_ALERTES_EQUIPE_DESTINATAIRES")
    monkeypatch.delenv(M._ENV_ALERTE, raising=False)
    assert M.alertes_credential()["actif"] is False
    assert M.run(["alertes-credential"]) == 0
    monkeypatch.setenv(M._ENV_ALERTE, "1")
    assert M.alertes_credential(dry_run=True)["envoyes"] == 0
    assert M.run(["alertes-credential"], dry_run=True) == 0

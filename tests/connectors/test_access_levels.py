"""Niveaux d'accès d'un connecteur OAuth : la déclaration, la couverture, le réglage.

Conception validée le 14/10/2026 (dépôt connectors, `docs/conception/niveaux-d-acces.md`).
Bancs PURS : le réglage est lu par `access_levels.settings`, doublé ici ; la requête elle-même
(`connector_settings.scoped_settings_for_key`) est éprouvée contre PostgreSQL par
`test_access_level_live.py`.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import zoho as zoho_oauth
from oto_mcp.connectors import access_levels as al

CRM, DESK = al.DECLARED["zoho"], al.DECLARED["zohodesk"]


# --- full == le consentement d'avant, à l'octet ----------------------------------

def test_full_est_exactement_le_consentement_d_avant():
    """CLIQUET. `full` ne filtre rien et demande ce que le consentement demandait avant
    les niveaux : ces tuples sont les valeurs de `auth/zoho.SCOPES` avant le 14/10/2026,
    recopiées ici exprès — c'est la seule liste écrite à la main de ces bancs."""
    assert zoho_oauth.SCOPES == {
        "zoho": ("ZohoCRM.modules.ALL", "ZohoCRM.settings.modules.READ",
                 "ZohoCRM.settings.fields.READ", "ZohoCRM.users.READ"),
        "zohodesk": ("Desk.articles.READ", "Desk.basic.READ", "Desk.search.READ",
                     "Desk.tickets.READ", "Desk.contacts.READ"),
        "zohoanalytics": ("ZohoAnalytics.data.read", "ZohoAnalytics.metadata.read"),
    }
    # Une seule source : les scopes `full` du consentement SONT ceux de la déclaration.
    for c in ("zoho", "zohodesk"):
        assert zoho_oauth.SCOPES[c] is al.DECLARED[c].levels[al.FULL]
        assert zoho_oauth.consent_scopes(c, al.FULL) == zoho_oauth.SCOPES[c]


def test_read_demande_les_scopes_decides():
    assert zoho_oauth.consent_scopes("zoho", "read") == (
        "ZohoCRM.modules.READ", "ZohoCRM.settings.modules.READ")
    assert zoho_oauth.consent_scopes("zohodesk", "read") == (
        "Desk.articles.READ", "Desk.search.READ")


def test_analytics_n_a_pas_de_niveaux():
    assert al.declared("zohoanalytics") is None
    assert zoho_oauth.consent_scopes("zohoanalytics", al.FULL) == zoho_oauth.SCOPES["zohoanalytics"]
    with pytest.raises(zoho_oauth.ZohoOAuthError):
        zoho_oauth.consent_scopes("zohoanalytics", "read")


# --- la couverture ----------------------------------------------------------------

def test_l_implication_est_une_donnee_declaree():
    """`ZohoCRM.modules.ALL` couvre les quatre verbes PARCE QUE la déclaration le dit."""
    assert {"ZohoCRM.modules.READ", "ZohoCRM.modules.CREATE", "ZohoCRM.modules.UPDATE",
            "ZohoCRM.modules.DELETE"} <= CRM.closure(al.FULL)
    assert CRM.closure("read") == {"ZohoCRM.modules.READ", "ZohoCRM.settings.modules.READ"}


def test_read_ne_couvre_que_la_lecture_du_crm():
    assert CRM.covered_ops("read", "zoho_record") == {"list", "get", "search"}
    assert CRM.covered_ops("read", "zoho_note") == {"list"}
    assert CRM.covers("read", "zoho_modules")
    assert CRM.covers("read", "zoho_connect")      # le geste de consentement, toujours
    assert CRM.covered_ops(al.FULL, "zoho_record") == {
        "list", "get", "search", "create", "update", "delete"}


def test_read_du_desk_sert_les_articles_et_la_recherche():
    couverts = {t for t in DESK.tools if DESK.covers("read", t)}
    assert couverts == {"zohodesk_articles", "zohodesk_article", "zohodesk_search_tickets"}


def test_une_op_ou_un_outil_non_declare_n_est_pas_couvert():
    """Fail-closed : ce qui n'est pas déclaré n'est jamais servi sous un niveau restreint."""
    assert not CRM.covers(al.FULL, "zoho_record", "purge")
    assert not CRM.covers(al.FULL, "zoho_record", None)
    assert not CRM.covers(al.FULL, "zoho_inconnu")


def test_la_monotonie_se_lit_sur_les_scopes():
    assert CRM.narrower_or_equal("read", al.FULL)
    assert not CRM.narrower_or_equal(al.FULL, "read")
    assert CRM.narrower_or_equal("read", "read")


# --- la validation d'une déclaration -------------------------------------------

@pytest.mark.parametrize("data", [
    {"levels": {"read": ("A",)}, "tools": {}},                       # pas de full
    {"levels": {"full": ()}, "tools": {}},                           # niveau vide
    {"levels": {"full": ("A",)}, "tools": {"t": {}}},                # outil sans op
    {"levels": {"full": ["A"]}, "tools": {}},                        # pas un tuple
    {"levels": {"full": ("A",)}, "tools": {}, "scopes": ("A",)},     # clé inconnue
])
def test_une_declaration_mal_formee_leve(data):
    with pytest.raises(ValueError):
        al._valider("x", data)


# --- la résolution : équipe > org > full, et l'org plafonne ------------------------

def _reglages(monkeypatch, **par_connecteur):
    monkeypatch.setattr(al, "settings", lambda org, group: par_connecteur)


def test_sans_reglage_c_est_full(monkeypatch):
    _reglages(monkeypatch)
    assert al.resolve("zoho", 1, 2) == (al.FULL, "default")


def test_l_org_puis_l_equipe(monkeypatch):
    _reglages(monkeypatch, zoho={"org": "read"})
    assert al.resolve("zoho", 1, 2) == ("read", "org")
    _reglages(monkeypatch, zoho={"group": "read"})
    assert al.resolve("zoho", 1, 2) == ("read", "group")


def test_une_equipe_plus_large_que_son_org_ne_s_applique_pas(monkeypatch):
    """L'org a restreint APRÈS que l'équipe a posé `full` : l'org est le plafond."""
    _reglages(monkeypatch, zoho={"org": "read", "group": "full"})
    assert al.resolve("zoho", 1, 2) == ("read", "org")


def test_un_connecteur_sans_niveaux_est_full():
    assert al.resolve("serper", 1, 2) == (al.FULL, "default")


def test_une_valeur_inconnue_en_base_leve():
    with pytest.raises(ValueError):
        al._stored("zoho", "org", "admin")


def test_poser_un_niveau_non_declare_est_refuse():
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("org", 1, "zoho", "admin", "sub")
    assert e.value.code == "unknown_access_level"
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("org", 1, "serper", "read", "sub")
    assert e.value.code == "no_access_levels"


def test_une_equipe_ne_peut_pas_elargir(monkeypatch):
    from oto_mcp.db import connector_settings
    ecrit = []
    monkeypatch.setattr(connector_settings, "set_connector_setting",
                        lambda *a: ecrit.append(a))
    _reglages(monkeypatch, zoho={"org": "read"})
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("group", 7, "zoho", al.FULL, "sub", org_id=1)
    assert e.value.code == "access_level_wider_than_org"
    assert not ecrit, "un refus n'écrit rien"
    al.set_level("group", 7, "zoho", "read", "sub", org_id=1)
    assert ecrit == [("group", "7", "zoho", al.KEY, "read", "sub")]

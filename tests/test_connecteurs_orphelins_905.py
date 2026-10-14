"""Une référence à un connecteur absent du catalogue servi se SIGNALE (#905).

Deux faces : le chargement (sélection, exposition, credentials, réglages) journalise en
erreur chaque nom orphelin, une fois par (table, nom) et par processus, sans rien changer
à ce qu'il rend ; le constat (`oto_admin_monitoring op=connecteurs_orphelins`) liste, en
lecture seule, les orphelins de chaque table qui nomme un connecteur.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from oto_mcp import credentials_store
from oto_mcp.capabilities import monitoring
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.connectors import activation, orphelins, selection
from oto_mcp.db import connector_settings

FANTOME = "connecteur_disparu_905"
CONNU = "serper"

CTX = ResolvedCtx(sub="sub-admin")


@pytest.fixture(autouse=True)
def _memoire_neuve(monkeypatch):
    """Chaque banc part d'un processus qui n'a encore rien signalé."""
    monkeypatch.setattr(orphelins, "_SIGNALES", set())


def _erreurs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == orphelins.__name__ and r.levelno == logging.ERROR]


class _Conn:
    """Une connexion qui rend `rows` à toute requête — le chargement seul est testé ici."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None


def _connect_rendant(rows):
    @contextmanager
    def _connect(*_a, **_k):
        yield _Conn(rows)
    return _connect


# ── signaler : une erreur nommée, une fois par (table, nom) et par processus ──

def test_un_nom_absent_du_catalogue_est_une_erreur_qui_nomme_table_et_connecteur(caplog):
    assert CONNU in orphelins.catalogue()
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        vus = orphelins.signaler("user_selected_connectors", [CONNU, FANTOME])
    assert vus == {FANTOME}
    (message,) = _erreurs(caplog)
    assert "user_selected_connectors" in message and repr(FANTOME) in message


def test_une_fois_par_table_et_par_nom(caplog):
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        for _ in range(3):
            assert orphelins.signaler("connector_settings", [FANTOME]) == {FANTOME}
        orphelins.signaler("connector_credentials", [FANTOME])
    messages = _erreurs(caplog)
    assert len(messages) == 2
    assert "connector_settings" in messages[0] and "connector_credentials" in messages[1]


def test_un_catalogue_resolu_ne_dit_rien(caplog):
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        assert orphelins.signaler("connector_availability", [CONNU]) == frozenset()
    assert _erreurs(caplog) == []


# ── le chargement signale, et rend ses lignes telles quelles ──────────────────

def test_la_selection_chargee_signale_et_rend_tout(monkeypatch, caplog):
    from oto_mcp import db
    rows = [{"connector": CONNU, "state": "active", "origin": "membre"},
            {"connector": FANTOME, "state": "active", "origin": "kit"}]
    monkeypatch.setattr(db, "_connect", _connect_rendant(rows))
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        assert selection.list_selection("sub-x", 7) == {CONNU: "active", FANTOME: "active"}
        assert set(selection.list_selection_detail("sub-x", 7)) == {CONNU, FANTOME}
    (message,) = _erreurs(caplog)
    assert "user_selected_connectors" in message


def test_l_exposition_chargee_signale(monkeypatch, caplog):
    from oto_mcp import db
    rows = [{"scope_type": "platform", "connector": CONNU, "enabled": True},
            {"scope_type": "platform", "connector": FANTOME, "enabled": True}]
    monkeypatch.setattr(db, "_connect", _connect_rendant(rows))
    monkeypatch.setattr(activation, "tenant_of_org", lambda *_a, **_k: None)
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        assert activation.exposed_connectors(7) == {CONNU, FANTOME}
    (message,) = _erreurs(caplog)
    assert "connector_availability" in message


def test_les_credentials_charges_signalent_sauf_un_remote(monkeypatch, caplog):
    rows = [{"connector": FANTOME, "account": "", "secret_kind": "api_key", "set_by": None,
             "set_at": None, "meta": {}},
            {"connector": "pont_maison", "account": "", "secret_kind": "api_key",
             "set_by": None, "set_at": None, "meta": {"base_url": "https://pont.exemple.test"}}]
    monkeypatch.setattr(credentials_store, "_connect", _connect_rendant(rows))
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        rendu = credentials_store.list_credentials("org", "7")
    assert [r["connector"] for r in rendu] == [FANTOME, "pont_maison"]
    (message,) = _erreurs(caplog)
    assert "connector_credentials" in message and repr(FANTOME) in message


def test_les_reglages_charges_signalent(monkeypatch, caplog):
    rows = [{"scope_type": "platform", "scope_id": "platform", "connector": FANTOME,
             "key": "cardinality", "value": "multi", "set_by": None, "set_at": None}]
    monkeypatch.setattr(connector_settings, "_connect", _connect_rendant(rows))
    with caplog.at_level(logging.ERROR, logger=orphelins.__name__):
        assert connector_settings.list_connector_settings() == rows
        assert connector_settings.org_settings_for_key("7", "k") == {
            FANTOME: "multi"}
    (message,) = _erreurs(caplog)
    assert "connector_settings" in message


# ── le constat : une op de la console admin, sans filtre ──────────────────────

def test_l_op_rend_le_constat(monkeypatch):
    attendu = {"catalogue": 1, "tables": [], "total": 0}
    monkeypatch.setattr(orphelins, "constat", lambda: attendu)
    inp = monitoring.MonitoringInput(op="connecteurs_orphelins")
    assert monitoring._monitoring(CTX, inp) is attendu


def test_l_op_refuse_un_filtre_qu_elle_ne_lit_pas():
    inp = monitoring.MonitoringInput(op="connecteurs_orphelins", org_id=7)
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, inp)
    assert e.value.code == "param_not_read_by_op"


def test_le_constat_couvre_chaque_table_vivante():
    tables = [t for t, _ in orphelins.REFERENCES]
    assert tables == ["user_selected_connectors", "connector_selection_removed",
                      "connector_availability", "connector_credentials",
                      "connector_settings", "connector_instances",
                      "orgs.default_connectors"]
    for _, sql in orphelins.REFERENCES:
        assert sql.lstrip().upper().startswith("SELECT")


def test_le_constat_sur_la_base_reelle(live):
    from oto_mcp import db
    with db._connect() as conn:
        org = conn.execute("INSERT INTO orgs (name) VALUES ('banc-orphelins') "
                           "RETURNING id").fetchone()["id"]
        conn.execute("UPDATE orgs SET default_connectors = %s WHERE id = %s",
                     ([CONNU, FANTOME], org))
        for nom in (CONNU, FANTOME):
            conn.execute("INSERT INTO user_selected_connectors (sub, org_id, connector) "
                         "VALUES ('sub-a', %s, %s), ('sub-b', %s, %s)", (org, nom, org, nom))
            conn.execute("INSERT INTO connector_selection_removed (sub, org_id, connector) "
                         "VALUES ('sub-c', %s, %s)", (org, nom))
            conn.execute("INSERT INTO connector_availability (scope_type, scope_id, "
                         "connector, enabled) VALUES ('org', %s, %s, true)", (str(org), nom))
            conn.execute("INSERT INTO connector_settings (scope_type, scope_id, connector, "
                         "key, value) VALUES ('org', %s, %s, 'k', 'v')", (str(org), nom))
            conn.execute("INSERT INTO connector_credentials (entity_type, entity_id, "
                         "connector, secret_enc) VALUES ('org', %s, %s, 'x')", (str(org), nom))
            conn.execute("INSERT INTO connector_instances (connector, owner_type, owner_id) "
                         "VALUES (%s, 'org', %s)", (nom, str(org)))
        # Un pont remote se définit par sa donnée : ni lui ni son instance ne comptent.
        conn.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                     "secret_enc, meta) VALUES ('org', %s, 'pont_maison', 'x', %s)",
                     (str(org), '{"base_url": "https://pont.exemple.test"}'))
        conn.execute("INSERT INTO connector_instances (connector, owner_type, owner_id) "
                     "VALUES ('pont_maison', 'org', %s)", (str(org),))
        # Une instance archivée n'est plus une référence vivante.
        conn.execute("INSERT INTO connector_instances (connector, owner_type, owner_id, "
                     "account, revoked_at) VALUES (%s, 'org', %s, 'ancien', NOW())",
                     (FANTOME, str(org)))
    rendu = orphelins.constat()
    par_table = {t["table"]: t["connecteurs"] for t in rendu["tables"]}
    assert par_table == {
        "user_selected_connectors": [{"connecteur": FANTOME, "lignes": 2}],
        "connector_selection_removed": [{"connecteur": FANTOME, "lignes": 1}],
        "connector_availability": [{"connecteur": FANTOME, "lignes": 1}],
        "connector_credentials": [{"connecteur": FANTOME, "lignes": 1}],
        "connector_settings": [{"connecteur": FANTOME, "lignes": 1}],
        "connector_instances": [{"connecteur": FANTOME, "lignes": 1}],
        "orgs.default_connectors": [{"connecteur": FANTOME, "lignes": 1}],
    }
    assert rendu["total"] == 8
    assert rendu["catalogue"] == len(orphelins.catalogue())

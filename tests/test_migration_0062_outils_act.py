"""La révision `0062_outils_act` réécrit les listes d'outils enregistrées (17/10/2026).

`gmail_message`, `gmail_compose`, `gmail_list_accounts`, `outlook_message` et
`outlook_compose` ne sont plus servis : chaque liste qui les cite — preset de projet,
flotte, déclencheur, masquage ou démasquage — doit GARDER SON SENS sous les nouveaux noms
(`outils_retires.remplacants`). Joué sur une vraie base : ce qu'on vérifie, c'est ce que
le stockage porte après la montée.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "0061_valeurs_materialisees"
_REVISION = "0062_outils_act"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


@pytest.fixture(scope="module")
def base_montee(live, pg_module_dsn):
    """Une base bootée, estampillée à la révision d'avant, peuplée de listes qui citent
    les anciens noms — puis montée par la révision."""
    import psycopg
    from alembic import command

    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("INSERT INTO projects (id, owner_id, name, mcp_tools) VALUES "
                  "(90001, 'u1', 'p', ARRAY['serper_search','gmail_message',"
                  "'gmail_compose']), (90002, 'u1', 'q', ARRAY['fr_get'])")
        c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools) "
                  "VALUES (90001, 1, 'u1', 'f', 'p', %s::jsonb)",
                  (json.dumps(["outlook_compose", "data_write"]),))
        for jid, statut in ((90001, "pending"), (90002, "done")):
            c.execute("INSERT INTO runner_jobs (id, org_id, kind, status, payload) VALUES "
                      "(%s, 1, 'start', %s, %s::jsonb)",
                      (jid, statut, json.dumps({"tools": ["gmail_compose", "fr_get"],
                                                "procedure": "p"})))
        c.execute("INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES "
                  "('u1', 0, 'gmail_compose'), ('u1', 0, 'gmail_act')")
        c.execute("INSERT INTO org_disabled_tools (org_id, tool_name, disabled_by) VALUES "
                  "(7, 'outlook_message', 'admin')")
        c.execute("INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES "
                  "('u2', 3, 'gmail_list_accounts')")
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_une_liste_qui_autorise_garde_ses_droits_sur_place(base_montee):
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 90001") == [
        (["serper_search", "gmail", "gmail_act"],)]
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 90002") == [
        (["fr_get"],)], "une liste qui ne cite aucun ancien nom n'est pas touchée"
    assert _lire(base_montee, "SELECT tools FROM runner_fleets WHERE id = 90001") == [
        (["outlook", "outlook_act", "data_write"],)]


def test_un_travail_vivant_suit_l_histoire_reste(base_montee):
    """La liste d'un travail pas encore terminé est celle que le serveur tient à chaque
    appel (#1256) : réécrite. Un travail terminé est l'histoire : intact."""
    assert _lire(base_montee, "SELECT payload FROM runner_jobs WHERE id = 90001") == [
        ({"tools": ["gmail", "gmail_act", "fr_get"], "procedure": "p"},)]
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 90002") \
        == [(["gmail_compose", "fr_get"],)]


def test_un_masquage_masque_tous_les_remplacants_sans_doublon(base_montee):
    assert sorted(_lire(base_montee, "SELECT tool_name FROM user_disabled_tools "
                                     "WHERE sub = 'u1'")) == [("gmail",), ("gmail_act",)]
    assert sorted(_lire(base_montee, "SELECT tool_name, disabled_by FROM org_disabled_tools "
                                     "WHERE org_id = 7")) == [
        ("outlook", "admin"), ("outlook_act", "admin")]
    assert _lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                              "WHERE sub = 'u2'") == [("gmail",)]


def test_aucun_ancien_nom_ne_reste(base_montee):
    from oto_mcp import outils_retires

    anciens = sorted({r.ancien for r in outils_retires.RENOMMAGES_ACT})
    for table in ("user_disabled_tools", "user_enabled_tools", "org_disabled_tools",
                  "group_disabled_tools"):
        assert _lire(base_montee, f"SELECT tool_name FROM {table} "
                                  "WHERE tool_name = ANY(%s)", anciens) == [], table
    assert _lire(base_montee, "SELECT id FROM projects WHERE mcp_tools && %s::text[]",
                 anciens) == []
    assert _lire(base_montee, "SELECT id FROM runner_fleets WHERE tools ?| %s::text[]",
                 anciens) == []

"""La révision `20261021_1219_outils_act_msg` réécrit les listes d'outils enregistrées
qui citent les anciens noms des messageries (21/10/2026).

`whatsapp_chat`, `whatsapp_send`, `linkedin_unipile_chat`, `teams_spaces`,
`chat_send`… ne sont plus servis. Décision d'Alexis du 21/10/2026 : une liste qui
AUTORISE un ancien lecteur ne reçoit que le nouveau lecteur, jamais `<ns>_act` ; seul un
ancien outil d'envoi dédié donne `<ns>_act` ; une liste qui MASQUE masque tout. Joué sur
une vraie base, estampillée à la révision d'avant, comme une base de prod où 0062 est
déjà passée.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "0072_triggers_hook_sources"
_REVISION = "20261021_1219_outils_act_msg"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


@pytest.fixture(scope="module")
def base_montee(live, pg_module_dsn):
    import psycopg
    from alembic import command

    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("INSERT INTO projects (id, owner_id, name, mcp_tools) VALUES "
                  "(91001, 'u1', 'p', ARRAY['whatsapp_chat','serper_search',"
                  "'whatsapp_send']), (91002, 'u1', 'q', ARRAY['gmail','fr_get']), "
                  "(91003, 'u1', 'r', ARRAY['whatsapp_chat'])")
        c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools) "
                  "VALUES (91001, 1, 'u1', 'f', 'p', %s::jsonb)",
                  (json.dumps(["teams_spaces", "teams_message", "data_write"]),))
        for jid, statut in ((91001, "held"), (91002, "failed")):
            c.execute("INSERT INTO runner_jobs (id, org_id, kind, status, payload) VALUES "
                      "(%s, 1, 'start', %s, %s::jsonb)",
                      (jid, statut, json.dumps({"tools": ["linkedin_unipile_chat"],
                                                "procedure": "p"})))
        c.execute("INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES "
                  "('u1', 0, 'chat_send')")
        c.execute("INSERT INTO org_disabled_tools (org_id, tool_name, disabled_by) VALUES "
                  "(7, 'telegram_chat', 'admin')")
        c.execute("INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES "
                  "('u2', 3, 'teams_message')")
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_un_ancien_lecteur_autorise_ne_donne_que_la_lecture(base_montee):
    """`whatsapp_chat` envoyait encore en transition : c'est la faille. Autorisé seul,
    il devient `whatsapp` seul."""
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 91003") == [
        (["whatsapp"],)]
    assert _lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                              "WHERE sub = 'u2'") == [("teams",)]


def test_un_ancien_outil_d_envoi_dedie_donne_l_acteur(base_montee):
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 91001") == [
        (["whatsapp", "serper_search", "whatsapp_act"],)]
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 91002") == [
        (["gmail", "fr_get"],)], "une liste qui ne cite aucun ancien nom n'est pas touchée"
    assert _lire(base_montee, "SELECT tools FROM runner_fleets WHERE id = 91001") == [
        (["teams", "data_write"],)]


def test_un_travail_vivant_suit_l_histoire_reste(base_montee):
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 91001") \
        == [(["linkedin_unipile"],)]
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 91002") \
        == [(["linkedin_unipile_chat"],)]


def test_un_masquage_masque_tous_les_remplacants(base_montee):
    """Masquer reste large : l'ancien lecteur masqué masque aussi l'acteur."""
    assert _lire(base_montee, "SELECT tool_name FROM user_disabled_tools "
                              "WHERE sub = 'u1'") == [("chat_act",)]
    assert sorted(_lire(base_montee, "SELECT tool_name, disabled_by FROM org_disabled_tools "
                                     "WHERE org_id = 7")) == [
        ("telegram", "admin"), ("telegram_act", "admin")]

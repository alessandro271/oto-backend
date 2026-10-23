"""La révision `20261023_0713_lemlist_personas` réécrit les listes d'outils enregistrées
du lot des personas lemlist (23/10/2026).

`lemlist_database` reste servi et perd `persona_create` / `persona_delete`, passés à
`lemlist_act`. Masquer reste large : une liste qui masque `lemlist_database` masque aussi
`lemlist_act`. Une liste qui avait `lemlist_act` (reçu de `lemlist_inbox_send`) le garde :
il porte désormais aussi les personas (choix assumé, aucun agent vivant ne perd l'envoi).
Joué sur une vraie base, estampillée à la révision d'avant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261022_2112_forfait_fenetre"
_REVISION = "20261023_0713_lemlist_personas"


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
                  "(93001, 'u1', 'p', ARRAY['lemlist_database','serper_search']), "
                  "(93002, 'u1', 'q', ARRAY['lemlist_act','lemlist_inbox']), "
                  "(93003, 'u1', 'r', ARRAY['gmail'])")
        c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools) "
                  "VALUES (93001, 1, 'u1', 'f', 'p', %s::jsonb)",
                  (json.dumps(["lemlist_database", "lemlist_act"]),))
        for jid, statut in ((93001, "pending"), (93002, "failed")):
            c.execute("INSERT INTO runner_jobs (id, org_id, kind, status, payload) VALUES "
                      "(%s, 1, 'start', %s, %s::jsonb)",
                      (jid, statut, json.dumps({"tools": ["lemlist_act", "fr_get"],
                                                "procedure": "p"})))
        c.execute("INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES "
                  "('u1', 0, 'lemlist_database')")
        c.execute("INSERT INTO group_disabled_tools (group_id, tool_name, disabled_by) "
                  "VALUES (7, 'lemlist_database', 'admin')")
        c.execute("INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES "
                  "('u2', 3, 'lemlist_database'), ('u2', 3, 'lemlist_act')")
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_le_masquage_de_l_acteur_suit_celui_de_la_base(base_montee):
    assert sorted(_lire(base_montee, "SELECT tool_name FROM user_disabled_tools "
                                     "WHERE sub = 'u1'")) == [("lemlist_act",),
                                                              ("lemlist_database",)]
    assert sorted(_lire(base_montee, "SELECT tool_name, disabled_by FROM group_disabled_tools "
                                     "WHERE group_id = 7")) == [("lemlist_act", "admin"),
                                                                ("lemlist_database", "admin")]


def test_l_acteur_recu_de_l_outil_d_envoi_est_garde(base_montee):
    """`lemlist_act`, que le lot des envois a donné à la place de `lemlist_inbox_send`,
    reste là où il était autorisé ; le lecteur autorisé n'est pas touché."""
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 93002") == [
        (["lemlist_act", "lemlist_inbox"],)]
    assert _lire(base_montee, "SELECT tools FROM runner_fleets WHERE id = 93001") == [
        (["lemlist_database", "lemlist_act"],)]
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 93001") \
        == [(["lemlist_act", "fr_get"],)]
    assert sorted(_lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                                     "WHERE sub = 'u2'")) == [("lemlist_act",),
                                                              ("lemlist_database",)]
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 93001") == [
        (["lemlist_database", "serper_search"],)]

"""La révision `20261021_1352_outils_act_envois` réécrit les listes d'outils enregistrées
du lot des envois (21/10/2026).

Deux formes : l'outil d'envoi DÉDIÉ retiré (`linkedin_unipile_publish`,
`lemlist_inbox_send`…) ; le lecteur MIXTE (`apollo_email`, `linkedin_unipile_post`…) qui
reste servi et perd ses ops d'envoi. Décision du 21/10/2026 : autoriser ne donne jamais
plus qu'avant — une liste qui autorise un lecteur mixte n'est pas touchée, un outil dédié
ne donne `<ns>_act` que si celui-ci ne fait rien de plus (`lemlist_inbox_send` →
`lemlist_act`), sinon RIEN (`linkedin_unipile_publish`, `_invite`) ; une liste qui MASQUE
masque tout ce que l'outil faisait. Joué sur une vraie base, estampillée à la révision
d'avant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261021_1219_outils_act_msg"
_REVISION = "20261021_1352_outils_act_envois"


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
                  "(92001, 'u1', 'p', ARRAY['apollo_email','serper_search']), "
                  "(92002, 'u1', 'q', ARRAY['linkedin_unipile_publish','fr_get',"
                  "'linkedin_unipile_post']), "
                  "(92003, 'u1', 'r', ARRAY['gmail']), "
                  # Le lot des messageries joué seul avant : l'acteur LinkedIn donné pour
                  # la messagerie, que ce lot élargit.
                  "(92004, 'u1', 's', ARRAY['linkedin_unipile_act','linkedin_unipile'])")
        c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools) "
                  "VALUES (92001, 1, 'u1', 'f', 'p', %s::jsonb)",
                  (json.dumps(["lemlist_inbox_send", "lemlist_inbox"]),))
        for jid, statut in ((92001, "pending"), (92002, "failed")):
            c.execute("INSERT INTO runner_jobs (id, org_id, kind, status, payload) VALUES "
                      "(%s, 1, 'start', %s, %s::jsonb)",
                      (jid, statut, json.dumps({"tools": ["linkedin_unipile_invite",
                                                          "signwell_document"],
                                                "procedure": "p"})))
        c.execute("INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES "
                  "('u1', 0, 'pennylane_invoice')")
        c.execute("INSERT INTO group_disabled_tools (group_id, tool_name, disabled_by) "
                  "VALUES (5, 'linkedin_unipile_publish', 'admin'), "
                  "(6, 'linkedin_unipile_act', 'admin')")
        c.execute("INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES "
                  "('u2', 3, 'lemlist_inbox_send'), ('u2', 3, 'ahrefs_social'), "
                  "('u2', 3, 'linkedin_unipile_invite'), ('u3', 3, 'linkedin_unipile_act')")
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_un_lecteur_mixte_autorise_reste_seul(base_montee):
    """`apollo_email` envoyait par `op="send"` : autorisé, il reste lui-même et ne donne
    jamais `apollo_act`."""
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 92001") == [
        (["apollo_email", "serper_search"],)]
    assert sorted(_lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                                     "WHERE sub = 'u2'")) == [("ahrefs_social",),
                                                              ("lemlist_act",)]


def test_un_outil_d_envoi_dedie_donne_l_acteur_qui_ne_fait_rien_de_plus(base_montee):
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 92002") == [
        (["fr_get", "linkedin_unipile_post"],)], \
        "`linkedin_unipile_act` fait plus que publier : l'autorisation se perd"
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 92003") == [
        (["gmail"],)], "une liste qui ne cite aucun nom du lot n'est pas touchée"
    assert _lire(base_montee, "SELECT tools FROM runner_fleets WHERE id = 92001") == [
        (["lemlist_act", "lemlist_inbox"],)]


def test_un_travail_vivant_suit_l_histoire_reste(base_montee):
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 92001") \
        == [(["signwell_document"],)]
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 92002") \
        == [(["linkedin_unipile_invite", "signwell_document"],)]


def test_l_acteur_elargi_n_est_plus_autorise(base_montee):
    """Si le lot des messageries a été joué seul avant, `linkedin_unipile_act` autorisé
    ne valait que pour la messagerie : il se perd ; masqué, il reste masqué."""
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 92004") == [
        (["linkedin_unipile"],)]
    assert _lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                              "WHERE sub = 'u3'") == []
    assert _lire(base_montee, "SELECT tool_name FROM group_disabled_tools "
                              "WHERE group_id = 6") == [("linkedin_unipile_act",)]


def test_un_masquage_masque_aussi_l_acteur(base_montee):
    """Masquer reste large : le lecteur mixte masqué reste masqué et masque l'acteur ;
    l'outil d'envoi dédié masqué masque l'acteur."""
    assert sorted(_lire(base_montee, "SELECT tool_name FROM user_disabled_tools "
                                     "WHERE sub = 'u1'")) == [("pennylane_act",),
                                                              ("pennylane_invoice",)]
    assert _lire(base_montee, "SELECT tool_name, disabled_by FROM group_disabled_tools "
                              "WHERE group_id = 5") == [("linkedin_unipile_act", "admin")]

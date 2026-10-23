"""Un agent DATE ses changements : `runner_triggers.updated_at`, en base.

Ce qu'une doublure ne peut pas prouver, c'est QUI écrit la date : toute écriture de
configuration (allumer, éteindre, changer l'horaire, transférer, l'adresse d'un webhook)
la pose, et le tick qui enfile une occurrence ne la touche JAMAIS — sans quoi un agent
quotidien se lirait « changé ce matin » chaque matin, et la date ne daterait plus rien.
Joué sur une vraie base (`live`).
"""
from __future__ import annotations

import pytest

ORG = 8300


def _relire(rt, tid):
    return rt.get_trigger(tid, ORG)["updated_at"]


def _vieillir(tid):
    """Recule la date posée, pour qu'une écriture suivante se distingue d'elle."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE runner_triggers SET updated_at = NOW() - interval '1 day' "
                     "WHERE id = %s", (tid,))


@pytest.fixture()
def agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 20 * * *", next_due="2000-01-01 00:00:00+00")
    return rt, t


def test_un_agent_neuf_ne_porte_aucun_changement(agent):
    rt, t = agent
    assert t["updated_at"] is None
    assert t["created_at"]


def test_allumer_et_changer_l_horaire_datent_l_agent(agent):
    rt, t = agent
    rt.update_trigger(t["id"], ORG, {"enabled": False})
    eteint = _relire(rt, t["id"])
    assert eteint, "éteindre est un changement"
    _vieillir(t["id"])
    vieux = _relire(rt, t["id"])
    rt.update_trigger(t["id"], ORG, {"cron": "55 18 * * *", "enabled": True})
    assert _relire(rt, t["id"]) > vieux, "rallumer et retimer redatent"


def test_le_tick_ne_date_pas_l_agent(agent):
    """Enfiler une occurrence n'est pas changer l'agent : `last_enqueued_at` le dit."""
    rt, t = agent
    assert rt.consume_due(t["id"], None, "2999-01-01 00:00:00+00")
    relu = rt.get_trigger(t["id"], ORG)
    assert relu["last_enqueued_at"]
    assert relu["updated_at"] is None


def test_une_retouche_vide_ne_date_rien(agent):
    rt, t = agent
    rt.update_trigger(t["id"], ORG, {})
    assert _relire(rt, t["id"]) is None


def test_le_transfert_date_l_agent(agent):
    rt, t = agent
    rt.reprendre_trigger(t["id"], ORG, "john")
    assert _relire(rt, t["id"])


def test_l_adresse_d_un_webhook_date_l_agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="hook", tz="UTC", tools=["a"],
                          kind="webhook")
    assert t["updated_at"] is None
    assert rt.poser_adresse_de_hook(t["id"], ORG, "h_" + "0" * 32)
    assert _relire(rt, t["id"])

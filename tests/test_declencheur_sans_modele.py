"""Un déclencheur dont l'agent ne déclare aucun modèle n'ENFILE plus (21/10/2026).

Son travail n'aurait aucune famille : aucun worker ne le servirait, et la réservation
l'arrêterait aussitôt (`runner_jobs._SANS_MODELE`). La livraison est donc refusée et
motivée sur le déclencheur, par le canal des livraisons refusées (`refused_no_model`),
comme une org suspendue ou une procédure archivée. Le déclencheur n'est pas éteint.

Une famille déclarée mais NON servie, elle, s'enfile toujours : c'est sa péremption
qui en dira la cause (`test_raison_peremption_db.py`).

Sans base : chaque banc passe par `_tick` / `declencher` et n'assert que sur ce qu'ils
écrivent.
"""
from __future__ import annotations

import contextlib
import logging

import pytest

from oto_mcp import runner_hook, runner_models, runner_tick

MOTIF = "cet agent ne déclare aucun modèle : il ne sera pas exécuté"


def _declencheur(**kw):
    t = {"id": 7, "org_id": 2, "sub": "u1", "cron": "0 * * * *", "tz": "UTC",
         "next_due": "2026-10-21 08:00:00", "procedure": "veille", "project_id": None,
         "enabled": True}
    t.update(kw)
    return t


@pytest.fixture
def tick(monkeypatch):
    """Le tick sur UN déclencheur, org en service, procédure en service. Rend la liste
    des livraisons écrites et celle des travaux enfilés."""
    ecrit, enfiles = [], []

    def _arme(t):
        monkeypatch.setattr(runner_tick.db, "due_triggers", lambda: [t])
        return ecrit, enfiles

    monkeypatch.setattr(runner_tick.db, "consume_due", lambda *a: True)
    monkeypatch.setattr(runner_tick.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_tick.procedure_retiree, "archivee", lambda *a: None)
    monkeypatch.setattr(runner_tick.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_tick.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue, k)))
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, sub=None, payload=None: enfiles.append(payload))
    monkeypatch.setattr(runner_tick.db, "runner_arme",
                        lambda org: {"armed": True, "families": []})
    monkeypatch.setattr(runner_tick._abonnement, "raison_de_peremption",
                        lambda *a, **k: "raison")
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda *a, **k: 0)
    return _arme


def test_le_motif_est_celui_du_catalogue():
    assert runner_models.motif_sans_famille(None) == MOTIF
    assert runner_models.motif_sans_famille("") == MOTIF
    assert runner_models.motif_sans_famille("mistral-large-2512") is None


def test_le_tick_n_enfile_pas_un_agent_sans_modele(tick, caplog):
    ecrit, enfiles = tick(_declencheur())
    with caplog.at_level(logging.WARNING, logger=runner_tick.log.name):
        assert runner_tick._tick() == 0
    assert enfiles == []
    assert ecrit == [(7, "refused_no_model", {"source": "schedule"})]
    assert MOTIF in caplog.text


def test_le_tick_nomme_un_modele_sorti_du_catalogue(tick, caplog):
    """Un modèle déclaré puis retiré du catalogue n'a pas de famille non plus — mais
    le dire « sans modèle » enverrait chercher le mauvais geste."""
    ecrit, enfiles = tick(_declencheur(model="modele-retire-9"))
    with caplog.at_level(logging.WARNING, logger=runner_tick.log.name):
        assert runner_tick._tick() == 0
    assert enfiles == []
    assert [e[1] for e in ecrit] == ["refused_no_model"]
    assert "`modele-retire-9`" in caplog.text and "il ne sera pas exécuté" in caplog.text


def test_le_tick_enfile_toujours_une_famille_non_servie(tick):
    """(c) Aucun worker ne sert `mistral` : on enfile quand même, avec sa famille —
    c'est la péremption qui dira pourquoi personne ne l'a pris."""
    ecrit, enfiles = tick(_declencheur(model="mistral-large-2512"))
    assert runner_tick._tick() == 1
    assert ecrit == []
    (charge,) = enfiles
    assert charge["model_family"] == "mistral"


def _hook(monkeypatch, t):
    ecrit = []
    monkeypatch.setattr(runner_hook, "_authentifier", lambda *a, **k: (t, None, None))
    monkeypatch.setattr(runner_hook.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_hook.procedure_retiree, "archivee", lambda *a: None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_hook.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue)))
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne doit être enfilé"))
    return ecrit


def test_le_webhook_refuse_un_agent_sans_modele_et_le_motive(monkeypatch):
    ecrit = _hook(monkeypatch, _declencheur())
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(7, "secret", None)
    assert (e.value.statut, e.value.code) == (409, "trigger_no_model")
    assert e.value.message == ("This agent declares no model: it will not run until one is set."
                               " The delivery is recorded and visible on its page.")
    assert ecrit == [(7, "refused_no_model")]


def test_un_agent_en_pause_reste_refuse_comme_en_pause(monkeypatch):
    """L'ordre des refus ne bouge pas : un agent éteint se dit éteint d'abord."""
    ecrit = _hook(monkeypatch, _declencheur(enabled=False))
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(7, "secret", None)
    assert e.value.code == "trigger_paused"
    assert ecrit == [(7, "refused_paused")]

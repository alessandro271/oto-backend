"""Un outil appelé par `oto_call` doit laisser la MÊME trace de facturation qu'un appel
direct (plan « Fuites de crédits », faille F2).

Constaté le 2026-09-10 en lisant le code : `oto_call` exécute la cible hors middleware,
puis `_trace_target_call` écrivait sa ligne sous le nom cible SANS `key_mode`, SANS
`quantity`, SANS `instance`, et avec l'org relue APRÈS le reset des axes — l'org maison
de l'appelant, pas celle où la cible avait résolu ses credentials. Ce que la cible
consignait tombait dans le relevé de la requête enveloppe, donc sur la ligne
`tool='oto_call'` — que la lentille de facturation (`org.usage.calls`, filtrée par nom
d'outil) ne lit jamais. Résultat : `linkedin_aiark_search` via `oto_call` n'était
jamais facturé, sous aucune org.

Ces bancs pilotent le VRAI `oto_call` (enregistré sur un FastMCP de test) avec une
cible factice qui consigne ce qu'un tool keyed consigne en production.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from oto_mcp import calllog, session_org
from oto_mcp.tools import meta

ORG_MAISON = 999


def _oto_call():
    from fastmcp import FastMCP

    m = FastMCP("t")
    meta.register(m)
    return asyncio.run(m.get_tool("oto_call"))


class _Axe:
    """Un axe d'appel factice : pose la ContextVar que le vrai axe poserait (après sa
    garde d'appartenance, hors sujet ici) et rend de quoi la défaire."""

    def __init__(self, param, poser, defaire):
        self.param, self._poser, self._defaire = param, poser, defaire

    async def pin_for(self, value, name):
        return [(self._defaire, self._poser(value))]


@pytest.fixture
def banc(monkeypatch):
    lignes: list[dict] = []
    monkeypatch.setattr(meta.db, "insert_tool_call", lambda row: lignes.append(row))
    monkeypatch.setattr(meta, "current_user_sub_from_token", lambda: "u-1")
    # L'org lue par le seam : celle posée par l'axe tant qu'il tient, la maison après.
    monkeypatch.setattr(meta.access, "current_org",
                        lambda sub: session_org.current_call_org() or ORG_MAISON)
    monkeypatch.setattr(meta.call_axes, "axes_for_call", lambda name: [
        _Axe("_org", lambda v: session_org.set_call_org(int(v)), session_org.reset_call_org),
        _Axe("_run_id", lambda v: session_org.set_call_run(str(v)), session_org.reset_call_run),
    ])

    async def _pas_d_org_de_run():
        return []

    monkeypatch.setattr(meta.run_org, "pin_for_call", _pas_d_org_de_run)
    monkeypatch.setattr(meta.redaction, "extract_payload", lambda result: result)
    monkeypatch.setattr(meta.redaction, "redact_payload",
                        lambda service, payload: meta.redaction.PASSTHROUGH)
    return lignes


def _cible(monkeypatch, run):
    async def _resolve(ctx, name):
        return SimpleNamespace(name=name, parameters={}, run=run)

    monkeypatch.setattr(meta, "_resolve_tool", _resolve)


def _appeler(outer: dict, **kwargs):
    """L'appel tel que le middleware le vit : un relevé ENVELOPPE posé autour."""
    tok = session_org.set_call_trace(outer)
    try:
        return asyncio.run(_oto_call().fn(ctx=SimpleNamespace(), **kwargs))
    finally:
        session_org.reset_call_trace(tok)


def test_la_cible_d_oto_call_porte_cle_quantite_et_org_de_resolution(monkeypatch, banc):
    async def run(args):
        session_org.note_call_trace(key_mode="platform", quantity=47,
                                    instance="platform:aiark",
                                    resolved_connector="linkedin_aiark",
                                    resolved_account="acme")
        return {"content": []}

    _cible(monkeypatch, run)
    outer: dict = {}
    _appeler(outer, name="linkedin_aiark_search", arguments={"op": "people"}, _org=178)

    (ligne,) = banc
    assert ligne["tool"] == "linkedin_aiark_search"
    assert ligne["org_id"] == 178, "l'org relue APRÈS le reset des axes est l'org maison"
    assert (ligne["key_mode"], ligne["quantity"]) == ("platform", 47)
    assert ligne["args"]["instance"] == "platform:aiark"
    assert ligne["ok"] is True


def test_l_enveloppe_garde_l_echo_mais_ne_porte_rien_qui_facture(monkeypatch, banc):
    """Une seule ligne par consommation : la cible. L'écho du compte (lu dans le relevé
    enveloppe par `CallContextMiddleware`) doit, lui, survivre au dispatch."""
    async def run(args):
        session_org.note_call_trace(key_mode="platform", quantity=3,
                                    resolved_connector="linkedin_aiark",
                                    resolved_account="acme")
        return {"content": []}

    _cible(monkeypatch, run)
    outer: dict = {}
    _appeler(outer, name="linkedin_aiark_search", arguments={}, _org=178)

    assert outer.get("resolved_account") == "acme"
    assert "key_mode" not in outer and "quantity" not in outer
    # et la ligne qu'en tirerait le sink de l'enveloppe ne facture rien
    from oto_mcp import server
    enveloppe = calllog.apply_call_trace({"tool": "oto_call"}, outer, server._TRACED_ARGS)
    assert "key_mode" not in enveloppe and "quantity" not in enveloppe


def test_une_cible_en_echec_trace_quand_meme_sous_la_bonne_org(monkeypatch, banc):
    async def run(args):
        session_org.note_call_trace(key_mode="platform")
        raise RuntimeError("AI Ark 502")

    _cible(monkeypatch, run)
    out = _appeler({}, name="linkedin_aiark_search", arguments={}, _org=178)

    assert out["ok"] is False
    (ligne,) = banc
    assert (ligne["ok"], ligne["org_id"]) == (False, 178)
    assert "AI Ark 502" in ligne["error"]


def test_le_run_de_la_cible_est_conserve(monkeypatch, banc):
    """Le jeton `_run_id=` passé à `oto_call` est lu AVANT le reset des axes : la ligne
    reste rattachée à son déroulé, comme un appel direct."""
    async def run(args):
        return {"content": []}

    _cible(monkeypatch, run)
    _appeler({}, name="linkedin_aiark_search", arguments={}, _org=178, _run_id="run-9")

    (ligne,) = banc
    assert ligne["run_id"] == "run-9"


def test_un_zero_trace_reste_zero_sur_la_cible(monkeypatch, banc):
    async def run(args):
        session_org.note_call_trace(key_mode="platform", quantity=0)
        return {"content": []}

    _cible(monkeypatch, run)
    _appeler({}, name="linkedin_aiark_search", arguments={}, _org=178)

    (ligne,) = banc
    assert ligne["quantity"] == 0


def test_apres_le_dispatch_le_releve_courant_est_de_nouveau_celui_de_l_enveloppe(
        monkeypatch, banc):
    """Ce que le sink du middleware relira APRÈS `oto_call` — donc ce qui atterrit sur
    la ligne `tool='oto_call'`.

    Le banc précédent vérifie le CONTENU du holder d'enveloppe. Celui-ci vérifie que
    c'est encore LUI que le contexte désigne. Sans le `reset_call_trace` posé en fin de
    dispatch, le relevé de la CIBLE reste installé : `current_call_trace()` le rend, le
    sink le verse, et la même consommation est facturée une SECONDE fois — sur la ligne
    d'enveloppe, celle-là même que cette PR vient de vider. Un banc qui n'inspecte que
    `outer` ne peut pas le voir : `outer` n'a pas bougé, c'est la ContextVar qui pointe
    ailleurs.

    Tout tient dans UN SEUL contexte (un seul `asyncio.run`) : c'est la condition pour
    que la fuite soit observable ici comme en production, où le handler async d'`oto_call`
    tourne dans le contexte du middleware (`await call_next`). Sous `asyncio.run` par
    appel — comme `_appeler` —, chaque `.set()` meurt avec la tâche et la garde ne peut
    pas être éprouvée."""
    async def run(args):
        session_org.note_call_trace(key_mode="platform", quantity=47,
                                    resolved_account="acme")
        return {"content": []}

    _cible(monkeypatch, run)
    outil = _oto_call()
    outer: dict = {}

    async def scenario():
        tok = session_org.set_call_trace(outer)
        try:
            await outil.fn(ctx=SimpleNamespace(), name="linkedin_aiark_search",
                           arguments={}, _org=178)
            return session_org.current_call_trace()
        finally:
            session_org.reset_call_trace(tok)

    courant = asyncio.run(scenario())

    assert courant is outer, ("le relevé de la CIBLE est resté installé après le "
                              "dispatch : le sink de l'enveloppe va le relire")
    from oto_mcp import server
    enveloppe = calllog.apply_call_trace({"tool": "oto_call"}, courant,
                                         server._TRACED_ARGS)
    assert "quantity" not in enveloppe and "key_mode" not in enveloppe, \
        "la consommation de la cible serait facturée une seconde fois sous `oto_call`"
    # et elle l'est bien UNE fois, sur la cible
    (ligne,) = banc
    assert (ligne["key_mode"], ligne["quantity"]) == ("platform", 47)


def _pile_de_session_qui_repond(monkeypatch):
    """Rend `get_context()` disponible ET la pile session bavarde. Sans contexte fastmcp
    — le cas de tous les autres bancs de ce fichier — `get_context()` lève, la pile
    n'est JAMAIS interrogée, et la précédence entre le jeton d'appel et la pile n'est
    pas exercée du tout."""
    async def _pile(c):
        return "run-de-la-pile"

    monkeypatch.setattr("fastmcp.server.dependencies.get_context",
                        lambda: SimpleNamespace(session_id="s-1"))
    monkeypatch.setattr(meta.guide_run, "active_run_id", _pile)


def test_le_jeton_de_run_de_l_appel_gagne_sur_la_pile_de_session(monkeypatch, banc):
    """`_run_id=` passé à `oto_call` PRIME la pile session de `guide_run` — c'est l'ordre
    qu'applique le sink du middleware (#108 : la pile session-scopée ne survit pas au
    renouvellement du Mcp-Session-Id), et la ligne de la cible doit suivre le même.

    Ici la pile RÉPOND, et elle répond autre chose : c'est ce qui distingue ce banc de
    `test_le_run_de_la_cible_est_conserve`, où un `run_id = await active_run_id(c)` sans
    le `run_id or` rendrait exactement le même résultat."""
    _pile_de_session_qui_repond(monkeypatch)

    async def run(args):
        return {"content": []}

    _cible(monkeypatch, run)
    _appeler({}, name="linkedin_aiark_search", arguments={}, _org=178, _run_id="run-9")

    (ligne,) = banc
    assert ligne["session_id"] == "s-1", "la pile n'était même pas atteignable"
    assert ligne["run_id"] == "run-9"


def test_sans_jeton_la_ligne_retombe_sur_la_pile_de_session(monkeypatch, banc):
    """La contre-épreuve du banc ci-dessus : le repli n'est pas mort. Sans lui, une ligne
    dispatchée dans un run ouvert sans `_run_id=` explicite perdrait son rattachement."""
    _pile_de_session_qui_repond(monkeypatch)

    async def run(args):
        return {"content": []}

    _cible(monkeypatch, run)
    _appeler({}, name="linkedin_aiark_search", arguments={}, _org=178)

    (ligne,) = banc
    assert ligne["run_id"] == "run-de-la-pile"

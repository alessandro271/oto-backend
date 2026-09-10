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

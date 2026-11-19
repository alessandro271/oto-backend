"""Crunchbase sur le connecteur de la lib (`oto.tools.crunchbase`), exécuté dans le
navigateur d'oto 1 (Browserbase), sur la session qu'oto 1 garde au coffre.

Ce que l'hôte doit tenir, et que ces bancs fixent :
- une session refusée (`SessionRejected`) marque la ligne de coffre RÉELLEMENT servie
  « à reconnecter » et le dit à l'agent (signaux #1070, #1076, #1149, #1163 : six jours
  d'expiration que seuls les runs voyaient) ;
- un login confirmé par la sonde de la lib est rendu par `on_jar`, qui l'écrit au coffre ;
- une sonde sans verdict (`ProbeUnavailable`) n'est jamais prise pour un succès : rien
  n'est écrit, et le refus le dit.

Aucun appel réel : le navigateur (Browserbase) et le coffre sont stubbés.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from oto_mcp import browser_session
from oto_mcp.access.resolved_credential import ResolvedCredential
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import crunchbase as cb


# --- lectures ---------------------------------------------------------------

@pytest.fixture
def banc(monkeypatch):
    marques: list = []
    appels: list = []
    rc = ResolvedCredential(provider="crunchbase", secret="ctx-123", is_platform=False,
                            mode="user", entity_type="member", entity_id="7:sub-x",
                            account="")
    monkeypatch.setattr(cb.browserbase, "is_configured", lambda: True)
    monkeypatch.setattr(cb.access, "resolve_credential", lambda *a, **k: rc)
    monkeypatch.setattr(cb.connector_health, "mark_rejected",
                        lambda *a: marques.append(a))
    reponse: dict = {}

    def _run(jar, page_url, page_function, arg=None):
        appels.append((jar, page_url, arg))
        if isinstance(reponse.get("leve"), Exception):
            raise reponse["leve"]
        return reponse

    monkeypatch.setattr(cb.browserbase, "run", _run)
    return reponse, marques, appels


@pytest.mark.parametrize("statut", [401, 403])
def test_une_session_refusee_marque_la_ligne_servie_et_le_dit(banc, statut):
    reponse, marques, _ = banc
    reponse.update(status=statut, data=None)
    with pytest.raises(McpError) as e:
        asyncio.run(cb._call(lambda c: c.search_companies("x")))
    assert len(marques) == 1
    assert marques[0][:4] == ("member", "7:sub-x", "crunchbase", "")
    assert str(statut) in marques[0][4]
    msg = str(e.value)
    assert "crunchbase_connect_start" in msg
    assert "no automatic reconnection" in msg
    assert "continue without this source" in msg


def test_une_lecture_passe_par_le_navigateur_de_l_hote_sur_la_session_du_coffre(banc):
    reponse, marques, appels = banc
    reponse.update(status=200, data={"entities": []})
    out = asyncio.run(cb._call(lambda c: c.search_companies("vector database", 5)))
    assert out == {"entities": []}
    assert marques == []
    jar, page_url, arg = appels[0]
    assert jar == "ctx-123", "le navigateur reçoit le Context gardé au coffre"
    assert page_url == "https://www.crunchbase.com/"
    assert "collection_ids=organization.companies" in arg["url"] and "limit=5" in arg["url"]


def test_une_fiche_se_lit_par_son_url_complete(banc):
    reponse, _, appels = banc
    reponse.update(status=200, data={"properties": {}})
    asyncio.run(cb._call(lambda c: c.get_company(
        "https://www.crunchbase.com/organization/anthropic")))
    assert "/entities/organizations/anthropic?" in appels[0][2]["url"]


def test_une_panne_serveur_n_est_pas_une_session_morte(banc):
    """Un 500 ne dit rien de la session : la marquer peindrait en rouge une session
    vivante sur un hoquet de Crunchbase."""
    reponse, marques, _ = banc
    reponse.update(status=500, data="boom")
    with pytest.raises(McpError) as e:
        asyncio.run(cb._call(lambda c: c.get_person("someone")))
    assert e.value.error.code == INTERNAL_ERROR and "500" in str(e.value)
    assert marques == []


def test_un_argument_invalide_est_refuse_sans_ouvrir_de_navigateur(banc):
    _, marques, appels = banc
    with pytest.raises(McpError) as e:
        asyncio.run(cb._call(lambda c: c.search_people("x", 26)))
    assert e.value.error.code == INVALID_PARAMS
    assert appels == [] and marques == []


def test_une_panne_du_navigateur_heberge_est_dite(banc):
    reponse, marques, _ = banc
    reponse["leve"] = cb.browserbase.BrowserbaseError("session not running")
    with pytest.raises(McpError) as e:
        asyncio.run(cb._call(lambda c: c.get_funding_rounds("anthropic")))
    assert e.value.error.code == INTERNAL_ERROR and "Browserbase" in str(e.value)
    assert marques == []


# --- connexion --------------------------------------------------------------

@pytest.fixture
def fenetre(monkeypatch):
    """Une fenêtre de login émise pour `sub-x`, une sonde dont on règle le statut, un
    coffre qui enregistre ce qu'on lui écrit."""
    ecrits: list = []
    sonde: dict = {"status": 200}
    vus: list = []

    async def _eval(session_id, page_url, page_function, arg=None):
        vus.append(session_id)
        return sonde["status"]

    monkeypatch.setattr(browser_session.browserbase, "run_session_eval", _eval)
    monkeypatch.setattr(browser_session, "_persist",
                        lambda *a, **k: ecrits.append(a))
    key = ("sub-x", "ctx-new", "sess-1")
    monkeypatch.setitem(browser_session._PENDING, key, time.monotonic() + 60)
    return sonde, ecrits, vus


def _finaliser(**k):
    return asyncio.run(browser_session.finalize("sub-x", "crunchbase", "ctx-new",
                                                "sess-1", **k))


def test_un_login_confirme_est_rendu_par_on_jar_et_ecrit_au_coffre(fenetre):
    sonde, ecrits, vus = fenetre
    res = _finaliser()
    assert res.connected and res.reason == browser_session.LOGGED_IN and not res.warning
    assert vus == ["sess-1"], "la sonde tourne dans la fenêtre de login vivante"
    assert ecrits == [("sub-x", "crunchbase", "ctx-new", "sess-1", "member", None)]
    assert ("sub-x", "ctx-new", "sess-1") not in browser_session._PENDING


@pytest.mark.parametrize("statut,motif,retry", [
    (401, browser_session.AUTH_REJECTED, True),
    (0, browser_session.NO_SESSION, True),
])
def test_un_login_non_confirme_n_ecrit_rien(fenetre, statut, motif, retry):
    sonde, ecrits, _ = fenetre
    sonde["status"] = statut
    res = _finaliser()
    assert (res.connected, res.reason, res.retry) == (False, motif, retry)
    assert ecrits == []
    assert ("sub-x", "ctx-new", "sess-1") in browser_session._PENDING, \
        "la fenêtre reste valable : la personne peut finir de se connecter"


def test_une_sonde_sans_verdict_n_est_jamais_un_succes(fenetre):
    sonde, ecrits, _ = fenetre
    sonde["status"] = 500
    res = _finaliser()
    assert res.connected is False
    assert res.reason == browser_session.PROBE_UNAVAILABLE and res.retry is False
    assert "500" in res.detail and "force=true" in res.detail
    assert ecrits == [], "rien n'est écrit au coffre sur une sonde muette"


def test_force_ecrit_sans_sonder(fenetre):
    _, ecrits, vus = fenetre
    res = _finaliser(force=True)
    assert res.connected and res.reason == browser_session.FORCED and res.warning
    assert vus == [] and len(ecrits) == 1


def test_la_sonde_du_support_est_celle_de_la_lib(monkeypatch):
    """`sonder` (santé d'une instance) passe par le `verify` enregistré : c'est la sonde
    de la lib, et sa `ProbeUnavailable` est celle que `browser_session` rattrape."""
    async def _eval(session_id, page_url, page_function, arg=None):
        return 502
    monkeypatch.setattr(cb.browserbase, "run_session_eval", _eval)
    with pytest.raises(browser_session.ProbeUnavailable):
        asyncio.run(cb._verify_session("sess-9"))

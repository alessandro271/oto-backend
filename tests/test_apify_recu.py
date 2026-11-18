"""Le reçu d'un run Apify (`tools/apify_recu.py`) — sur CHAQUE réponse qui lance ou
lit un run : `run_id`, `dataset_id`, `status`, `usage_total_usd`, `cost_usd`, `billing`.

Verrouille : un dict reçoit `receipt` EN PLUS (le reste tel quel) ; la LISTE d'éléments
reste une liste (contrat servi : `waterfall/fournisseurs.py::_apify_items`) et le reçu
tient dans la ligne de tête du canal texte ; `cost_usd` est le coût FINAL tracé pour la
facture, jamais la part réservée d'un run pas fini ; `billing` dit qui paie.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from oto_mcp.tools import apify_recu

RUN = "HG7ML7M8z78YcAPEB"   # la FORME d'un id de run Apify, fictif
DS = "dsAbCdEf012345678"     # la FORME d'un id de dataset, fictif


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import apify, apify_actors

    m = FastMCP("t")
    apify.register(m)
    apify_actors.register(m)
    return asyncio.run(m.get_tool(name))


def _r(status, usd=0.0421):
    return {"data": {"id": RUN, "status": status, "usageTotalUsd": usd,
                     "defaultDatasetId": DS}}


@pytest.fixture(autouse=True)
def _sans_base():
    with patch("oto_mcp.tools.apify._hors_du_lanceur", return_value=False), \
            patch("oto_mcp.tools.apify._noter_lancement"), \
            patch("oto_mcp.tools.apify._plafond_par_defaut", return_value=5.0), \
            patch("oto_mcp.tools.apify._plafond_max", return_value=25.0), \
            patch("oto_mcp.tools.apify._borne_byok", return_value=1000.0), \
            patch("oto_mcp.tools.apify._org_de_l_appel", return_value=None), \
            patch("oto_mcp.tools.apify._org_qui_prete", return_value=None):
        yield


def _appel(outil, *, run=None, statut=None, items=None, abort=None,
           key_mode="platform", ligne=None, **kwargs):
    """Appelle `outil` sur un client Apify simulé, la clé de l'appel au rang `key_mode`.
    Rend `(sortie, avis)` — `avis` = chaque ligne posée en tête de la réponse."""
    with ExitStack() as pile:
        pile.enter_context(patch("oto_mcp.access.resolve_api_key",
                                 return_value=("fake-key", False)))
        inst = pile.enter_context(patch("oto.tools.apify.client.ApifyClient")).return_value
        inst.api_key = "fake-key"
        pile.enter_context(patch("oto_mcp.tools.apify.session_org.note_call_trace"))
        pile.enter_context(patch("oto_mcp.tools.apify.session_org.current_call_trace",
                                 return_value={"key_mode": key_mode,
                                               "credential_row": ligne}))
        avis = pile.enter_context(patch("oto_mcp.tools.apify.session_org.noter_avis"))
        inst.run.return_value = run
        inst.run_status.return_value = statut
        inst.abort_run.return_value = abort
        inst.dataset_items.return_value = [{"a": 1}] if items is None else items
        out = _tool(outil).fn(**kwargs)
    return out, [c.args[0] for c in avis.call_args_list]


def _recus(avis):
    return [a for a in avis if a.startswith("Apify receipt")]


# --- la LISTE reste une liste, le reçu en tête --------------------------------

@pytest.mark.parametrize("outil, kwargs", [
    ("apify_run_sync", {"actor_id": "apify/x"}),
    ("apify_linkedin_profiles", {"urls": ["https://www.linkedin.com/in/x"]}),
    ("apify_google_maps_places", {"search_terms": ["bakery"], "location": "Lyon"}),
])
def test_a_list_answer_stays_a_list_and_its_receipt_heads_the_text(outil, kwargs):
    out, avis = _appel(outil, run=_r("SUCCEEDED"), **kwargs)
    assert out == [{"a": 1}], "contrat servi : la LISTE des éléments, inchangée"
    (ligne,) = _recus(avis)
    for attendu in (f"run_id={RUN}", f"dataset_id={DS}", "status=SUCCEEDED",
                    "cost_usd=0.042100", "usage_total_usd=0.042100", "billing=credits"):
        assert attendu in ligne


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED"])
def test_a_run_that_did_not_succeed_carries_its_receipt_in_the_dict(status):
    out, avis = _appel("apify_run_sync", run=_r(status), actor_id="apify/x")
    assert out["dataset_id"] == DS
    assert out["receipt"] == {"run_id": RUN, "dataset_id": DS, "status": status,
                              "usage_total_usd": 0.0421, "cost_usd": 0.0421,
                              "billing": "credits"}
    assert _recus(avis) == [], "un dict porte son reçu : pas de doublon en tête"


# --- les dicts reçoivent `receipt` EN PLUS ------------------------------------

def test_run_status_adds_the_receipt_and_serves_the_rest_as_is():
    out, _ = _appel("apify_run_status", statut=_r("SUCCEEDED"), run_id=RUN)
    assert out["data"] == _r("SUCCEEDED")["data"]
    assert out["receipt"]["cost_usd"] == 0.0421
    assert out["receipt"]["billing"] == "credits"


def test_a_running_run_has_no_cost_yet_only_apify_s_figure_so_far():
    """« Jusqu'ici » n'est pas une facture : `cost_usd` reste nul tant que le run tourne."""
    out, _ = _appel("apify_run_status", statut=_r("RUNNING", 0.01), run_id=RUN)
    assert out["receipt"] == {"run_id": RUN, "dataset_id": DS, "status": "RUNNING",
                              "usage_total_usd": 0.01, "cost_usd": None,
                              "billing": "pending"}


def test_a_finished_run_whose_cost_is_not_final_is_pending_not_its_booked_part():
    run = _r("SUCCEEDED", 0.0002)
    run["data"].update({"chargedEventCounts": {"x": 5},
                        "accountedChargedEventCounts": {"x": 0}})   # sans prix lisible
    out, _ = _appel("apify_run_status", statut=run, run_id=RUN)
    assert out["receipt"]["cost_usd"] is None
    assert out["receipt"]["billing"] == "pending"
    assert out["receipt"]["usage_total_usd"] == 0.0002


def test_apify_run_and_abort_carry_the_receipt_pending():
    out, _ = _appel("apify_run", run=_r("READY", 0.0), actor_id="apify/x")
    assert out["receipt"]["run_id"] == RUN and out["receipt"]["dataset_id"] == DS
    assert out["receipt"]["billing"] == "pending" and out["receipt"]["cost_usd"] is None
    out, _ = _appel("apify_abort_run", abort=_r("ABORTING"), run_id=RUN)
    assert out["receipt"]["status"] == "ABORTING" and out["receipt"]["run_id"] == RUN


# --- qui paie ----------------------------------------------------------------

def test_on_an_own_key_apify_bills_the_account_and_the_cost_is_still_shown():
    out, _ = _appel("apify_run_status", statut=_r("SUCCEEDED"), run_id=RUN,
                    key_mode="byo_org")
    assert out["receipt"]["billing"] == "apify_account"
    assert out["receipt"]["cost_usd"] == 0.0421


@pytest.mark.parametrize("slug, attendu", [("primaire", "credits"),
                                           ("partenaire", "apify_account")])
def test_a_tenant_key_is_re_billed_only_when_it_is_the_primary_tenant_s(
        monkeypatch, slug, attendu):
    """Le prédicat unique du bord des crédits (`credits.rang_refacture`) : l'opérateur
    paie la clé du tenant PRIMAIRE et la refacture ; un autre tenant paie la sienne."""
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "primaire")
    out, _ = _appel("apify_run_status", statut=_r("SUCCEEDED"), run_id=RUN,
                    key_mode="tenant", ligne=("tenant", slug))
    assert out["receipt"]["billing"] == attendu


def test_a_platform_run_read_from_another_org_is_billed_to_its_launcher():
    with patch("oto_mcp.tools.apify._hors_du_lanceur", return_value=True):
        out, _ = _appel("apify_run_status", statut=_r("SUCCEEDED"), run_id=RUN)
    assert out["receipt"]["billing"] == "launching_org"


# --- pur, tolérant ------------------------------------------------------------

@pytest.mark.parametrize("run", [None, [], {}, {"id": 3, "status": None,
                                                 "usageTotalUsd": "x"}])
def test_the_receipt_never_raises_on_an_unreadable_run(run):
    r = apify_recu.recu(run, RUN, refacturee=True, cout=float("nan"))
    assert r["run_id"] == RUN and r["cost_usd"] is None and r["billing"] == "pending"
    assert apify_recu.ligne(r).startswith("Apify receipt")
    assert set(r) == {"run_id", "dataset_id", "status", "usage_total_usd", "cost_usd",
                      "billing"}
    assert r["billing"] in apify_recu.FACTURATIONS


def test_a_missing_dataset_is_said_null_and_no_read_again_hint():
    r = apify_recu.recu({"id": RUN, "status": "SUCCEEDED"}, RUN, refacturee=False)
    texte = apify_recu.ligne(r)
    assert "dataset_id=null" in texte and "apify_dataset_items" not in texte


# --- par le vrai montage : la ligne arrive EN TÊTE, la liste reste la sortie ----

@pytest.mark.asyncio
async def test_through_the_mount_the_receipt_heads_the_text_and_the_list_is_served(
        monkeypatch):
    """Pas un `noter_avis` simulé : le vrai `RappelContexteMiddleware` pose la ligne en
    tête du canal texte, et le bloc suivant reste la LISTE d'éléments."""
    from fastmcp import Client, FastMCP

    from oto_mcp import rappel_contexte
    from oto_mcp.middleware.rappel_contexte import (ConstatContexteMiddleware,
                                                    RappelContexteMiddleware)
    from oto_mcp.tools import apify

    monkeypatch.setattr(rappel_contexte, "pour_l_appel", lambda outil: (None, None))
    mcp = FastMCP("apify-recu")
    apify.register(mcp)
    mcp.add_middleware(RappelContexteMiddleware())
    mcp.add_middleware(ConstatContexteMiddleware())
    with ExitStack() as pile:
        pile.enter_context(patch("oto_mcp.access.resolve_api_key",
                                 return_value=("fake-key", False)))
        inst = pile.enter_context(patch("oto.tools.apify.client.ApifyClient")).return_value
        inst.api_key = "fake-key"
        pile.enter_context(patch("oto_mcp.tools.apify.session_org.note_call_trace"))
        pile.enter_context(patch("oto_mcp.tools.apify.session_org.current_call_trace",
                                 return_value={"key_mode": "platform"}))
        inst.run.return_value = _r("SUCCEEDED")
        inst.dataset_items.return_value = [{"a": 1}, {"a": 2}]
        async with Client(mcp) as c:
            res = await c.call_tool("apify_run_sync", {"actor_id": "apify/x"})
    textes = [b.text for b in res.content if getattr(b, "text", None) is not None]
    assert textes[0].startswith("Apify receipt — run_id=" + RUN)
    assert f"dataset_id={DS}" in textes[0] and "billing=credits" in textes[0]
    import json
    assert json.loads(textes[1]) == [{"a": 1}, {"a": 2}], "la LISTE, servie telle quelle"
    assert len(textes) == 2

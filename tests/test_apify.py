"""Connecteur Apify — actors de scraping hébergés (api.apify.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
surface MCP sous le namespace `apify`, la jointure tool↔client oto-core (garde
version-skew), la sonde « tester la connexion », et les deux endroits où le module
protège l'utilisateur : le 408 du mode synchrone doit ORIENTER vers le mode
asynchrone, et les garde-fous de coût doivent atteindre le client.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "apify_store_search", "apify_actors", "apify_actor",
    "apify_run_sync", "apify_run", "apify_run_status", "apify_abort_run",
    "apify_dataset_items",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import apify

    m = FastMCP("t")
    apify.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_apify_is_keyed_connector_platform_grant_only():
    c = providers.REGISTRY["apify"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # platform sur grant explicite (revente au crédit) : jamais en libre-service.
    assert c.platform_key_open is False
    assert c.default_quota == 0
    assert "apify" in providers.KEY_PROVIDERS
    assert c.category == "Prospecting"
    assert c.publisher_name == "Apify"


def test_apify_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["apify"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_apify_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "apify"
               for t in all_tools if t.startswith("apify_"))


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("apify")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.apify.client import ApifyClient
    for meth in ("store_search", "actors", "actor", "run_sync_dataset_items",
                 "run", "run_status", "abort_run", "dataset_items"):
        assert callable(getattr(ApifyClient, meth, None)), f"ApifyClient.{meth} manquant"


# --- garde-fous de coût & orientation sur 408 ---------------------------------

def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.apify.client.ApifyClient")
    return key, cls


def test_run_sync_forwards_cost_guards():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        _tool("apify_run_sync").fn(
            actor_id="apify/google-maps-scraper",
            run_input={"searchStringsArray": ["boulangerie Marseille"]},
            max_items=20, max_total_charge_usd=1.5,
        )

        kwargs = inst.run_sync_dataset_items.call_args.kwargs
        assert kwargs["max_items"] == 20
        assert kwargs["max_total_charge_usd"] == 1.5


def test_sync_timeout_points_to_the_async_path():
    """408 = le run dépasse 300 s : le message doit nommer la sortie de secours."""
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.run_sync_dataset_items.side_effect = UpstreamHTTPError(
            408, {"error": "timeout"}, service="apify")
        with pytest.raises(McpError, match="apify_run_status"):
            _tool("apify_run_sync").fn(actor_id="apify/x")


# --- la ligne FACTURÉE : le coût réel d'un run TERMINÉ, une fois par run -------
#
# `apify_run_status` est le seul outil qui lit le coût d'un run (`usageTotalUsd`).
# Il le trace en micro-dollars (unité de Jev) avec le run pour JOB de la ligne
# (`apify_run_id`, servi `job_id` par la lentille) — et SEULEMENT quand le run est
# dans un état terminal : avant, le chiffre est partiel, et un facturier qui garde
# le premier chiffre vu pour un job garderait celui-là.

RUN = "HG7ML7M8z78YcAPEB"   # la FORME d'un id de run Apify, fictif


def _statut(reponse, run_id=RUN, **kwargs):
    """Appelle `apify_run_status` sur une réponse amont donnée ; rend
    `(sortie, traces)` — `traces` = chaque `note_call_trace` de l'appel."""
    key, cls = _with_fake_client()
    with key, cls as client_cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
        client_cls.return_value.run_status.return_value = reponse
        out = _tool("apify_run_status").fn(run_id=run_id, **kwargs)
    return out, [c.kwargs for c in trace.call_args_list]


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"])
def test_a_finished_run_traces_its_cost_in_microdollars_and_its_run(status):
    """Un run en échec, coupé ou arrêté a coûté aussi : les quatre états terminaux."""
    reponse = {"data": {"id": RUN, "status": status, "usageTotalUsd": 0.0421,
                        "defaultDatasetId": "ds1"}}
    out, traces = _statut(reponse)
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]
    assert out == reponse, "la réponse est servie telle quelle"


@pytest.mark.parametrize("status", ["READY", "RUNNING", "TIMING-OUT", "ABORTING",
                                    "", "succeeded"])
def test_a_run_that_is_not_finished_traces_a_free_poll_without_its_job(status):
    """Le coût n'est pas final : la consultation trace 0 (gratuite, jamais NULL que le
    registre lirait 1) et SANS le run — un facturier qui garde le premier chiffre d'un
    job garderait sinon un partiel. `TIMING-OUT` et `ABORTING` sont des états de
    TRANSITION, pas des états terminaux."""
    _, traces = _statut({"data": {"id": RUN, "status": status, "usageTotalUsd": 0.5}})
    assert traces == [{"quantity": 0}]


@pytest.mark.parametrize("run", [
    {"id": RUN, "status": "SUCCEEDED"},                               # champ absent
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": None},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": "0.04"},      # pas un nombre
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": True},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": -0.01},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": float("nan")},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": float("inf")},
])
def test_a_finished_run_without_a_readable_cost_traces_0_and_says_so(run, caplog):
    """Un chiffre absent n'est pas un zéro facturé, un chiffre deviné serait facturé,
    et un chiffre muet serait perdu : 0 sans le run, et un avertissement qui nomme le
    run et le TYPE reçu — jamais la valeur."""
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces = _statut({"data": run})
    assert traces == [{"quantity": 0}]
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


@pytest.mark.parametrize("status", [None, ["SUCCEEDED"], 3])
def test_an_unreadable_status_traces_0_and_says_so(status, caplog):
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces = _statut({"data": {"id": RUN, "status": status, "usageTotalUsd": 0.5}})
    assert traces == [{"quantity": 0}]
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


@pytest.mark.parametrize("usd,micro", [
    (0, 0),                    # mesuré, rien à facturer : un 0 TRACÉ
    (0.0, 0),
    (1, 1_000_000),
    (0.000001, 1),
    (0.0000004, 1),            # arrondi au micro-dollar SUPÉRIEUR, comme Jev
    (3 * 1e-05, 30),           # 3.0000000000000004e-05 : pas 31
    (12.345678, 12_345_678),
])
def test_the_cost_is_rounded_up_to_the_microdollar_like_jev(usd, micro):
    from oto_mcp.tools import jev

    _, traces = _statut({"data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": usd}})
    assert traces == [{"quantity": micro, "apify_run_id": RUN}]
    assert micro == jev._microdollars(usd), "même unité et même arrondi que Jev"


def test_the_job_is_the_run_apify_names_else_the_one_that_was_asked():
    """L'id de la RÉPONSE fait foi ; sans lui, celui de l'appel — jamais une ligne
    facturable sans job, que le facturier ne saurait pas compter une seule fois."""
    fini = {"status": "SUCCEEDED", "usageTotalUsd": 0.01}
    _, traces = _statut({"data": {**fini, "id": RUN}}, run_id="  autre  ")
    assert traces == [{"quantity": 10000, "apify_run_id": RUN}]
    _, traces = _statut({"data": dict(fini)}, run_id=RUN)
    assert traces == [{"quantity": 10000, "apify_run_id": RUN}]


def test_a_flat_answer_is_read_like_the_enveloped_one():
    """Le client oto-core rend l'enveloppe `{data: {…}}` de l'API v2 ; un corps à plat
    se lit pareil plutôt que de cesser de facturer en silence."""
    _, traces = _statut({"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.002})
    assert traces == [{"quantity": 2000, "apify_run_id": RUN}]


@pytest.mark.parametrize("reponse", [{}, {"data": None}, {"data": []}, [], None])
def test_an_unreadable_answer_traces_0_says_so_and_does_not_break_the_call(reponse, caplog):
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        out, traces = _statut(reponse)
    assert traces == [{"quantity": 0}] and out == reponse
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


def test_a_refused_status_call_traces_nothing():
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
        client_cls.return_value.run_status.side_effect = UpstreamHTTPError(
            404, {"error": "not found"}, service="apify")
        with pytest.raises(McpError):
            _tool("apify_run_status").fn(run_id=RUN)
    trace.assert_not_called()


def test_every_other_apify_tool_traces_a_free_call():
    """Lancer, arrêter, lire : chacun trace 0 — jamais NULL, que le registre lirait 1 —
    même quand l'amont rend un run TERMINÉ avec son coût (`apify_run` avec
    `wait_for_finish`, `apify_abort_run`). Le coût d'un run se relève par
    `apify_run_status`, et par lui seul."""
    fini = {"data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.5,
                     "defaultDatasetId": "ds1"}}
    appels = {
        "apify_run": ("run", {"actor_id": "apify/x", "wait_for_finish": 60}),
        "apify_abort_run": ("abort_run", {"run_id": RUN}),
        "apify_run_sync": ("run_sync_dataset_items", {"actor_id": "apify/x"}),
        "apify_dataset_items": ("dataset_items", {"dataset_id": "ds1"}),
        "apify_actor": ("actor", {"actor_id": "apify/x"}),
        "apify_actors": ("actors", {}),
        "apify_store_search": ("store_search", {"search": "maps"}),
    }
    assert set(appels) == EXPECTED_TOOLS - {"apify_run_status"}
    for outil, (methode, kwargs) in appels.items():
        key, cls = _with_fake_client()
        with key, cls as client_cls, \
                patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
            getattr(client_cls.return_value, methode).return_value = fini
            _tool(outil).fn(**kwargs)
        assert [c.kwargs for c in trace.call_args_list] == [{"quantity": 0}], outil


def test_run_sync_says_its_run_cost_is_not_recorded(caplog):
    """L'appel synchrone ne rend que les éléments du dataset — ni id de run ni coût,
    et le client ne garde aucun en-tête : le coût n'est pas relevé, et le journal le
    DIT à chaque appel plutôt qu'un NULL facturé 1."""
    key, cls = _with_fake_client()
    with key, cls as client_cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace, \
            caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        client_cls.return_value.run_sync_dataset_items.return_value = [{"a": 1}]
        out = _tool("apify_run_sync").fn(actor_id="apify/x")
    assert out == [{"a": 1}]
    assert [c.kwargs for c in trace.call_args_list] == [{"quantity": 0}]
    assert any("apify_run_sync" in r.getMessage() and "not recorded" in r.getMessage()
               for r in caplog.records)


def test_the_run_reaches_the_journal_as_the_job_of_the_billed_line():
    """Bout en bout sur le VRAI relevé (pas un mock) : ce que l'outil consigne passe la
    liste fermée des args journalisés et la lentille le lit sous ce nom comme `job_id` ;
    l'argument `run_id`, lui, n'est PAS un nom de job — d'autres outils le portent pour
    autre chose (`run_finish`). Clé qui facture : gardée sur la ligne CIBLE d'un
    `oto_call`, jamais sur l'enveloppe."""
    from oto_mcp import server, session_org
    from oto_mcp.calllog import apply_call_trace, truncated_args
    from oto_mcp.db import usage as dbu
    from oto_mcp.tools import meta

    assert "apify_run_id" in server._TRACED_ARGS
    assert "apify_run_id" in dbu.BILLABLE_JOB_ARGS
    assert "apify_run_id" in meta._BILLING_TRACE_KEYS
    assert "run_id" not in dbu.BILLABLE_JOB_ARGS and "job_id" not in dbu.BILLABLE_JOB_ARGS

    key, cls = _with_fake_client()
    releve: dict = {}
    tok = session_org.set_call_trace(releve)
    try:
        with key, cls as client_cls:
            client_cls.return_value.run_status.return_value = {
                "data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.0421}}
            _tool("apify_run_status").fn(run_id=RUN)
    finally:
        session_org.reset_call_trace(tok)

    row = apply_call_trace(
        {"args": truncated_args({"run_id": RUN}, tool="apify_run_status")},
        releve, server._TRACED_ARGS)
    assert row["quantity"] == 42100
    assert row["args"] == {"run_id": RUN, "apify_run_id": RUN}

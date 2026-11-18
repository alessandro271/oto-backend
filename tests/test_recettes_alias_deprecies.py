"""Une recette qui nomme un outil par son ANCIEN nom (`deprecations.TOOLS`) est servie par
l'outil d'aujourd'hui, comme `oto_call` et le bord du protocole la serviraient : même
table, mêmes arguments, le journal compte l'alias, la réponse porte l'avis.

Le défaut d'origine : le pont des recettes-code cherchait `http_get` dans le catalogue,
où seul `api_get` est servi depuis le renommage du connecteur Custom API, et rendait
`unknown_tool` — une recette arrêtée pendant que le même appel passait par `oto_call`.

Sans base : le catalogue est le VRAI montage (`static_mcp`), seule l'exécution est une
doublure — c'est le jugement du pont (plateforme, écritures, lecture déclarée) qui est
éprouvé sur le vrai `api_get`. Le journal, lui, est éprouvé sur base
(`test_recettes_pont_live.py`)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from _mcp_app import static_mcp
from oto_mcp import deprecations, session_org
from oto_mcp.recipes import ecritures as ec
from oto_mcp.recipes import moteur, pont

SUB = "sub-recettes-alias"


@pytest.fixture(scope="module")
def catalogue():
    return static_mcp()


@pytest.fixture
def joues(monkeypatch):
    """Ce que le pont a EXÉCUTÉ : le nom passé à l'outil, et l'ancien nom que le journal
    lit pendant l'appel (`session_org.nom_deprecie_de_l_appel`)."""
    vus: list = []

    async def executer(tool, sub, outil, arguments):
        vus.append((tool.name, outil, session_org.nom_deprecie_de_l_appel(), arguments))
        return {"ok": True, "result": {"status": 200}, "quantity": 1}

    monkeypatch.setattr(pont, "_executer", executer)
    return vus


def _avec_avis(coro):
    """Joue `coro` sous le holder d'avis d'un appel MCP ; rend (réponse, avis)."""
    async def jouer():
        avis: list = []
        jeton = session_org.set_call_avis(avis)
        try:
            return await coro(), avis
        finally:
            session_org.reset_call_avis(jeton)
    return asyncio.run(jouer())


def test_http_get_est_un_alias_d_api_get():
    # Le cas réel, tant que l'alias est servi : s'il part, ce banc part avec lui.
    assert deprecations.tool_canonique("http_get") == "api_get"


@pytest.mark.parametrize("appeleur", ["lecture", "recette"])
def test_une_recette_qui_appelle_http_get_atteint_api_get(catalogue, joues, appeleur):
    appeler = (pont.appeleur_en_lecture(catalogue, SUB) if appeleur == "lecture" else
               pont.appeleur_de_recette(catalogue, SUB, datastore=None, ecrire=True))
    args = {"path": "/v1/backlinks"}

    async def deux_appels():
        a = await appeler("http_get", args)
        b = await appeler("http_get", args)
        return a, b

    (premier, second), avis = _avec_avis(deux_appels)
    assert premier == second == {"ok": True, "result": {"status": 200}, "quantity": 1}
    # Servi par l'outil d'aujourd'hui, sous son nom, mêmes arguments ; l'alias reste
    # lisible du journal pendant l'appel, et de lui seul.
    assert joues == [("api_get", "api_get", "http_get", args)] * 2
    assert session_org.nom_deprecie_de_l_appel() is None
    # L'avis d'`oto_call`, une fois pour toute l'exécution — pas une ligne par appel.
    assert avis == [deprecations.avis_d_appel("http_get")]


def test_le_nom_d_aujourd_hui_ne_porte_ni_alias_ni_avis(catalogue, joues):
    reponse, avis = _avec_avis(lambda: pont.appeleur_en_lecture(catalogue, SUB)(
        "api_get", {"path": "/v1"}))
    assert reponse["ok"] is True
    assert joues == [("api_get", "api_get", None, {"path": "/v1"})]
    assert avis == []


def test_un_nom_inconnu_reste_inconnu(catalogue, joues):
    reponse = asyncio.run(pont.appeleur_en_lecture(catalogue, SUB)("http_nope", {}))
    assert reponse["code"] == "unknown_tool" and joues == []


def test_une_ecriture_declaree_sous_l_ancien_nom_est_celle_d_aujourd_hui(monkeypatch):
    """`writes: ["<ancien>:create"]` déclare l'écriture de l'outil d'aujourd'hui : sans
    cela, l'appel passait de `unknown_tool` à `write_not_declared`. Le reçu compte sous
    le nom d'aujourd'hui."""
    monkeypatch.setitem(deprecations.TOOLS, "acme_crm_v0", "acme_crm")
    monkeypatch.setitem(ec.ECRITURES, "acme_crm", ec.Ecriture(
        frozenset({"search"}), frozenset({"create"})))
    vus = []

    async def resoudre(fastmcp, outil):
        return SimpleNamespace(name=outil, parameters={"properties": {"op": {}}}) \
            if outil == "acme_crm" else None

    async def executer(tool, sub, outil, arguments):
        vus.append((outil, session_org.nom_deprecie_de_l_appel()))
        return {"ok": True, "result": {}}

    from oto_mcp.tools import meta
    monkeypatch.setattr(meta, "resoudre_outil", resoudre)
    monkeypatch.setattr(pont, "_executer", executer)
    compteur = pont.compteur_neuf()
    reponse = asyncio.run(pont.appeleur_de_recette(
        None, SUB, datastore=None, ecrire=True,
        ecritures=frozenset({"acme_crm_v0:create"}), max_ecritures=2,
        compteur=compteur)("acme_crm_v0", {"op": "create", "name": "a"}))
    assert reponse == {"ok": True, "result": {}}
    assert vus == [("acme_crm", "acme_crm_v0")]
    assert compteur["writes"] == {"acme_crm:create": 1}


def test_une_recette_declarative_nommee_par_l_ancien_nom_atteint_l_outil(catalogue,
                                                                        monkeypatch):
    """Le moteur des recettes déclaratives traduit par la même table (`echantillon`, le
    même chemin que chaque page d'`executer`)."""
    vus = []

    async def appeler(tool, sub, outil, args):
        vus.append((tool.name, outil, session_org.nom_deprecie_de_l_appel()))
        return SimpleNamespace(ok=True, retenu=False, result=SimpleNamespace(
            structured_content={"items": [{"url": "x"}]}))

    monkeypatch.setattr(moteur, "_appeler", appeler)
    forme, avis = _avec_avis(lambda: moteur.echantillon(
        catalogue, SUB, "http_get", {"path": "/v1"}, items="items"))
    assert forme["items_found"] is True and forme["items"] == 1
    assert vus == [("api_get", "api_get", "http_get")]
    assert avis == [deprecations.avis_d_appel("http_get")]

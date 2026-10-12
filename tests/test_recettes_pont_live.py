"""Ce que le pont du bac à sable a le droit d'appeler (#1202, lot 1) : un outil de
connecteur DÉCLARÉ EN LECTURE, sous les gardes d'`oto_call` — et rien d'autre.

Sur un vrai store (`live`) : `meta.executer_cible` lit la base (axes, org du run). Le
dernier banc joue aussi le vrai bac à sable, et passe sans lui en le disant."""
from __future__ import annotations

import asyncio
import os

import pytest

SUB = "sub-recettes-pont"


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    return SUB


@pytest.fixture
def serveur(compte, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.auth import hooks
    from oto_mcp.tools.lecture import LECTURE
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    appels: list[str] = []
    m = FastMCP("t-recettes-pont")

    @m.tool(annotations=LECTURE)
    def acme_people(company: str = "") -> dict:
        appels.append("acme_people")
        return {"content": [{"name": f"{company} {i}"} for i in range(2)]}

    @m.tool()
    def acme_post(text: str = "") -> dict:
        appels.append("acme_post")
        return {"posted": True}
    return m, appels


def _appeler(m, outil, arguments):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_en_lecture(m, SUB)(outil, arguments))


def test_un_outil_en_lecture_est_appele_et_son_resultat_rendu(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_people", {"company": "acme"})
    assert reponse["ok"] and reponse["result"]["content"][0]["name"] == "acme 0"
    assert appels == ["acme_people"]


def test_un_outil_qui_ecrit_est_refuse_avant_tout_appel(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_post", {"text": "x"})
    assert reponse == {"ok": False, "code": "recipe_tool_not_read_only",
                       "message": reponse["message"]}
    assert appels == []


def test_un_outil_inconnu_ou_de_plateforme_est_refuse(serveur):
    m, appels = serveur
    assert _appeler(m, "acme_nope", {})["code"] == "unknown_tool"
    assert _appeler(m, "data_write", {})["code"] == "tool_not_allowed"
    assert appels == []


def test_des_arguments_refuses_par_l_outil_ne_levent_pas(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_people", {"company": {"pas": "une chaîne"}})
    assert reponse["ok"] is False and reponse["code"] in ("call_refused", "invalid_input")


def test_de_bout_en_bout_le_code_lit_un_outil_et_ne_peut_pas_en_ecrire(serveur):
    from oto_mcp.functions import executor, pont as pont_bac
    from oto_mcp.recipes import pont
    if not os.environ.get(executor.ENV_DIR):
        if os.environ.get("CI"):
            pytest.fail(f"{executor.ENV_DIR} absente")
        pytest.skip(f"{executor.ENV_DIR} absente : pas de bac à sable sur ce poste")
    m, appels = serveur
    sortie = asyncio.run(pont_bac.executer_avec_pont(
        sources={"r.py": (
            "async def run(oto, entree):\n"
            "    lu = await oto.call('acme_people', {'company': 'acme'})\n"
            "    try:\n"
            "        await oto.call('acme_post', {'text': 'x'})\n"
            "        ecrit = 'oui'\n"
            "    except oto.ToolError as e:\n"
            "        ecrit = e.code\n"
            "    return {'lus': len(lu['content']), 'ecrit': ecrit}\n")},
        entrypoint="r:run", appeler=pont.appeleur_en_lecture(m, SUB)))
    assert sortie["result"] == {"lus": 2, "ecrit": "recipe_tool_not_read_only"}
    assert appels == ["acme_people"]

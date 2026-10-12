"""Ce que le pont du bac à sable a le droit d'appeler (#1202, lot 1) : un outil de
connecteur DÉCLARÉ EN LECTURE, sous les gardes d'`oto_call` — et rien d'autre. Une
recette-code y ajoute `data_rows` et `data_write` (lot 2) : des appels ORDINAIRES de ces
outils, gardés et journalisés comme les autres.

Sur un vrai store (`live`) : `meta.executer_cible` lit la base (axes, org du run). Le
dernier banc joue aussi le vrai bac à sable, et passe sans lui en le disant."""
from __future__ import annotations

import asyncio
import os
import uuid

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


# ── recette-code : les tableaux passent par le chemin ordinaire des outils ───────────

SCHEMA = {"key": "slug", "fields": [{"key": "slug", "type": "text"},
                                    {"key": "name", "type": "text"}]}


@pytest.fixture
def tableaux(serveur, monkeypatch):
    """Le serveur, plus les VRAIS outils `data_*` ; l'acteur lu là où ils le lisent."""
    from oto_mcp import access, call_axes
    from oto_mcp.tools import datastore as outils_tableaux
    m, _ = serveur
    outils_tableaux.register(m)
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    return m


def _table() -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = f"recettes-pont-{uuid.uuid4().hex[:6]}"
    db.create_datastore("user", SUB, ns)
    make_store(SUB).set_schema(ns, SCHEMA)
    return ns


def _lignes(ns: str) -> list:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).cursor_rows(ns, limit=100)["rows"]


def _journal(outil: str, ns: str) -> list:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT ok FROM tool_calls WHERE sub = %s AND tool = %s "
            "AND args->>'datastore' = %s", (SUB, outil, ns)).fetchall()


def _de_recette(m, ns, outil, arguments):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_de_recette(m, SUB, datastore=ns, ecrire=True)(
        outil, arguments))


def test_data_write_du_pont_laisse_sa_ligne_dans_le_journal(tableaux):
    ns = _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"rows": [{"slug": "a", "name": "A"}], "key": "slug"})
    assert reponse["ok"] and reponse["result"]["inserted"] == 1
    assert [r["slug"] for r in _lignes(ns)] == ["a"]
    assert [r["ok"] for r in _journal("data_write", ns)] == [True]


def test_data_write_du_pont_est_refuse_quand_la_garde_de_l_outil_refuse(tableaux,
                                                                        monkeypatch):
    # Une org suspendue : la garde des outils de plateforme (`activation_gate`) refuse
    # `data_write`. Un accès direct au store, à côté d'`executer_cible`, ne la voyait pas.
    from oto_mcp import org_suspension
    monkeypatch.setattr(org_suspension, "refus", lambda org: "This organization is "
                                                             "suspended.")
    ns = _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"rows": [{"slug": "a", "name": "A"}], "key": "slug"})
    assert reponse["ok"] is False and reponse["code"] == "call_refused"
    assert org_suspension.CODE in reponse["message"]
    assert _lignes(ns) == []


def test_data_rows_du_pont_est_un_appel_journalise_et_son_refus_aussi(tableaux):
    from oto_mcp.datastore.core import make_store
    ns = _table()
    make_store(SUB).write_rows(ns, [{"slug": "a", "name": "A"}], key="slug")
    page = _de_recette(tableaux, ns, "data_rows", {"datastore": ns, "limit": 10})
    assert page["ok"] and [r["slug"] for r in page["result"]["rows"]] == ["a"]
    assert [r["ok"] for r in _journal("data_rows", ns)] == [True]
    absent = f"absent-{uuid.uuid4().hex[:6]}"
    refus = _de_recette(tableaux, ns, "data_rows", {"datastore": absent})
    assert refus["ok"] is False
    assert [r["ok"] for r in _journal("data_rows", absent)] == [False]


def test_data_write_du_pont_n_ecrit_que_dans_le_tableau_de_l_execution(tableaux):
    ns, autre = _table(), _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"datastore": autre, "rows": [{"slug": "x"}], "key": "slug"})
    assert reponse["code"] == "write_outside_target"
    assert _lignes(autre) == [] and _journal("data_write", autre) == []

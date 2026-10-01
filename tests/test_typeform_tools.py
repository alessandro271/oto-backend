"""Connecteur Typeform, en lecture seule — les trois outils `typeform_*`.

Les tripwires génériques couvrent le registre, l'éditeur, le logo, la prose
servie et la jointure au client oto-core. Ce fichier verrouille ce qui est
PROPRE à ce module, par le VRAI chemin FastMCP (`mcp.call_tool`), le client
remplacé par un double en mémoire :

- la région du credential choisit l'hôte, une région inconnue est refusée ;
- les vues resserrées (`full=True` rend le brut) et ce qu'elles nomment retiré ;
- les réponses lisibles : intitulé → valeur, homonymes départagés par l'id ;
- le refus d'un data center qui rendrait des réponses VIDES ;
- la pagination par `before`, la borne de page, les arguments ignorés refusés ;
- la traduction des refus amont (401/403/404) ; 429 et 5xx restent typés.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS

from oto_mcp import access
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import typeform as T

US = "https://api.typeform.com"
EU = "https://api.eu.typeform.com"

FORM = {
    "id": "f1", "title": "Satisfaction", "language": "fr",
    "_links": {"display": "https://acme.typeform.com/to/f1",
               "responses": f"{US}/forms/f1/responses"},
    "fields": [
        {"id": "q1", "ref": "nom", "type": "short_text", "title": "Votre nom ?",
         "validations": {"required": True}},
        {"id": "q2", "ref": "ville", "type": "multiple_choice", "title": "Ville ?",
         "properties": {"choices": [{"id": "c1", "label": "Lyon"},
                                    {"id": "c2", "label": "Paris"}],
                        "allow_multiple_selection": True}},
        {"id": "g1", "ref": "grp", "type": "group", "title": "Détails",
         "properties": {"fields": [
             {"id": "q3", "ref": "note", "type": "rating", "title": "Commentaire"},
             {"id": "q4", "ref": "autre", "type": "long_text", "title": "Commentaire"},
         ]}},
    ],
    "hidden": ["utm_source"],
    "logic": [{"type": "field"}], "settings": {"is_public": True},
    "welcome_screens": [{"title": "Bonjour"}],
}

RESPONSE = {
    "response_id": "r1", "token": "r1", "landing_id": "r1",
    "landed_at": "2026-09-30T10:00:00Z", "submitted_at": "2026-09-30T10:02:00Z",
    "metadata": {"user_agent": "Mozilla/5.0", "referer": "https://acme.test"},
    "hidden": {"utm_source": "newsletter"},
    "calculated": {"score": 0},
    "variables": [{"key": "score", "type": "number", "number": 4}],
    "answers": [
        {"field": {"id": "q1", "type": "short_text", "ref": "nom"},
         "type": "text", "text": "Jane Doe"},
        {"field": {"id": "q2", "type": "multiple_choice", "ref": "ville"},
         "type": "choices", "choices": {"labels": ["Lyon", "Paris"]}},
        {"field": {"id": "q3", "type": "rating", "ref": "note"},
         "type": "number", "number": 5},
        {"field": {"id": "q4", "type": "long_text", "ref": "autre"},
         "type": "text", "text": "RAS"},
    ],
}


class _FauxClient:
    """Double en mémoire du client oto-core : rend des pages, note les appels."""

    def __init__(self, access_token, region="us"):
        from oto.tools.typeform import REGIONS
        self.access_token = access_token
        self.region = region
        self.BASE_URL = REGIONS[region]
        self.appels = []
        self.form = dict(FORM)
        self.page = {"total_items": 1, "page_count": 1, "items": [RESPONSE]}
        self.leve = None

    def _note(self, nom, *args, **kw):
        self.appels.append((nom, args, kw))
        if self.leve:
            raise self.leve

    def list_workspaces(self, **kw):
        self._note("list_workspaces", **kw)
        return {"total_items": 1, "page_count": 1, "items": [
            {"id": "w1", "name": "Ventes", "account_id": "a1", "shared": False,
             "forms": {"count": 3, "href": f"{US}/workspaces/w1/forms"},
             "self": {"href": f"{US}/workspaces/w1"}}]}

    def list_forms(self, **kw):
        self._note("list_forms", **kw)
        return {"total_items": 1, "page_count": 1, "items": [
            {"id": "f1", "title": "Satisfaction", "created_at": "2026-01-01T00:00:00Z",
             "last_updated_at": "2026-09-01T00:00:00Z", "settings": {"is_public": True},
             "self": {"href": f"{US}/forms/f1"}, "theme": {"href": f"{US}/themes/t"},
             "_links": {"display": "https://acme.typeform.com/to/f1",
                        "responses": f"{US}/forms/f1/responses"}}]}

    def get_form(self, form_id):
        self._note("get_form", form_id)
        return self.form

    def list_responses(self, form_id, **kw):
        self._note("list_responses", form_id, **kw)
        return self.page


class _Banc:
    """Le serveur monté, les clients construits, les champs du credential, et
    `prepare(client)` — appliqué à chaque client construit."""
    prepare = None


@pytest.fixture
def banc(monkeypatch):
    b = _Banc()
    b.champs = {"key": "tfp_test"}
    b.construits = []
    monkeypatch.setattr(access, "resolve_credential_fields",
                        lambda provider, account=None: dict(b.champs))
    import oto.tools.typeform as pkg

    def _construire(**kw):
        c = _FauxClient(**kw)
        if b.prepare:
            b.prepare(c)
        b.construits.append(c)
        return c

    monkeypatch.setattr(pkg, "TypeformClient", _construire)
    b.mcp = FastMCP("banc-typeform")
    T.register(b.mcp)
    return b


def _appeler(b, outil, **arguments):
    return asyncio.run(b.mcp.call_tool(outil, arguments)).structured_content


def _refus(b, outil, **arguments) -> McpError:
    """Le refus, lu sur la fonction de l'outil : à travers `call_tool`, fastmcp
    l'enveloppe en `ToolError` et son code ne se lit plus."""
    fn = {t.name: t for t in asyncio.run(b.mcp._list_tools())}[outil].fn
    with pytest.raises(McpError) as e:
        fn(**arguments)
    assert e.value.error.code == INVALID_PARAMS
    return e.value


def test_trois_outils_en_lecture_seule(banc):
    noms = {t.name for t in asyncio.run(banc.mcp.list_tools(run_middleware=False))}
    assert noms == {"typeform_workspaces", "typeform_forms", "typeform_responses"}


# --- région -----------------------------------------------------------------

def test_region_absente_vise_les_us(banc):
    _appeler(banc, "typeform_forms")
    assert banc.construits[0].region == "us"
    assert banc.construits[0].access_token == "tfp_test"


def test_la_region_posee_choisit_lhote(banc):
    banc.champs["region"] = "EU "
    _appeler(banc, "typeform_forms")
    assert banc.construits[0].BASE_URL == EU


def test_une_region_inconnue_est_refusee(banc):
    banc.champs["region"] = "asia"
    _refus(banc, "typeform_forms")
    assert banc.construits == []


# --- workspaces & formulaires ------------------------------------------------

def test_workspaces_vue_resserree(banc):
    r = _appeler(banc, "typeform_workspaces", search="Ventes", page_size=5)
    assert r["workspaces"] == [{"id": "w1", "name": "Ventes", "shared": False,
                                "forms_count": 3, "account_id": "a1"}]
    assert banc.construits[0].appels == [
        ("list_workspaces", (), {"search": "Ventes", "page": None, "page_size": 5})]


def test_forms_list_resserree_et_brut(banc):
    r = _appeler(banc, "typeform_forms", workspace_id="w1", sort_by="last_updated_at")
    assert r["forms"][0] == {"id": "f1", "title": "Satisfaction",
                             "last_updated_at": "2026-09-01T00:00:00Z",
                             "created_at": "2026-01-01T00:00:00Z", "is_public": True,
                             "url": "https://acme.typeform.com/to/f1"}
    brut = _appeler(banc, "typeform_forms", full=True)
    assert "self" in brut["items"][0]


def test_forms_get_rend_les_questions_et_nomme_le_retire(banc):
    r = _appeler(banc, "typeform_forms", op="get", form_id="f1")
    assert [f["id"] for f in r["fields"]] == ["q1", "q2", "g1"]
    assert r["fields"][0]["required"] is True
    assert r["fields"][1]["choices"] == ["Lyon", "Paris"]
    assert r["fields"][1]["multiple"] is True
    assert [f["id"] for f in r["fields"][2]["fields"]] == ["q3", "q4"]
    assert "logic" not in r and "settings" not in r and "welcome_screens" not in r
    assert "logic" in r["omitted"]
    assert r["hidden"] == ["utm_source"]


def test_forms_get_sans_form_id_refuse(banc):
    _refus(banc, "typeform_forms", op="get")


@pytest.mark.parametrize("arg", [{"search": "x"}, {"page": 2}, {"workspace_id": "w"}])
def test_forms_get_refuse_un_argument_de_liste(banc, arg):
    _refus(banc, "typeform_forms", op="get", form_id="f1", **arg)


def test_forms_list_refuse_form_id(banc):
    _refus(banc, "typeform_forms", op="list", form_id="f1")


# --- réponses ----------------------------------------------------------------

def test_reponses_lisibles(banc):
    r = _appeler(banc, "typeform_responses", form_id="f1")
    assert r["total_items"] == 1 and r["form_title"] == "Satisfaction"
    rep = r["responses"][0]
    assert rep["response_id"] == "r1"
    assert rep["submitted_at"] == "2026-09-30T10:02:00Z"
    assert rep["answers"]["Votre nom ?"] == "Jane Doe"
    assert rep["answers"]["Ville ?"] == ["Lyon", "Paris"]
    # Deux sous-questions au même intitulé : départagées par l'id, aucune écrasée.
    assert rep["answers"]["Commentaire [q3]"] == 5
    assert rep["answers"]["Commentaire [q4]"] == "RAS"
    assert rep["hidden"] == {"utm_source": "newsletter"}
    assert rep["variables"] == {"score": 4}
    assert "metadata" not in rep and "score" not in rep
    assert "metadata" in r["omitted"]


def test_reponses_sans_intitules_par_ref_et_sans_lire_le_formulaire(banc):
    r = _appeler(banc, "typeform_responses", form_id="f1", titles=False)
    assert r["responses"][0]["answers"]["nom"] == "Jane Doe"
    assert [a[0] for a in banc.construits[0].appels] == ["list_responses"]


def test_reponses_brutes(banc):
    r = _appeler(banc, "typeform_responses", form_id="f1", full=True)
    assert r["items"][0]["metadata"]["referer"] == "https://acme.test"
    assert [a[0] for a in banc.construits[0].appels] == ["list_responses"]


def test_reponses_transmet_les_filtres(banc):
    _appeler(banc, "typeform_responses", form_id="f1", page_size=50,
             since="2026-09-01T00:00:00", response_type=["partial"], query="Lyon",
             fields=["q1"], answered_fields=["q2"], included_response_ids=["r1"])
    nom, args, kw = banc.construits[0].appels[-1]
    assert (nom, args) == ("list_responses", ("f1",))
    assert kw["page_size"] == 50 and kw["since"] == "2026-09-01T00:00:00"
    assert kw["response_type"] == ["partial"] and kw["query"] == "Lyon"
    assert kw["fields"] == ["q1"] and kw["answered_fields"] == ["q2"]
    assert kw["included_response_ids"] == ["r1"]


def test_page_pleine_donne_le_curseur_before(banc):
    def _deux(c):
        c.page = {"total_items": 7, "page_count": 4, "items": [
            dict(RESPONSE, response_id="r9", token="t9"),
            dict(RESPONSE, response_id="r8", token="t8")]}
    banc.prepare = _deux
    r = _appeler(banc, "typeform_responses", form_id="f1", page_size=2)
    assert r["next_before"] == "t8"
    banc.prepare = None
    r = _appeler(banc, "typeform_responses", form_id="f1", page_size=2)
    assert "next_before" not in r      # page incomplète : rien après


@pytest.mark.parametrize("taille", [0, T.RESPONSES_MAX_PAGE + 1])
def test_page_bornee(banc, taille):
    _refus(banc, "typeform_responses", form_id="f1", page_size=taille)


def test_before_et_after_ensemble_refuses(banc):
    _refus(banc, "typeform_responses", form_id="f1", before="a", after="b")


def test_data_center_qui_rendrait_vide_est_refuse(banc):
    def _compte_eu(c):
        c.form = dict(FORM, _links={"responses": f"{EU}/forms/f1/responses"})
    banc.prepare = _compte_eu
    e = _refus(banc, "typeform_responses", form_id="f1")
    assert "« eu »" in e.error.message      # la phrase EST le remède : elle nomme la région
    assert [a[0] for a in banc.construits[0].appels] == ["get_form"]


def test_meme_data_center_passe(banc):
    banc.champs["region"] = "eu"

    def _compte_eu(c):
        c.form = dict(FORM, _links={"responses": f"{EU}/forms/f1/responses"})
    banc.prepare = _compte_eu
    r = _appeler(banc, "typeform_responses", form_id="f1")
    assert r["responses"][0]["response_id"] == "r1"


# --- valeurs de réponse ------------------------------------------------------

@pytest.mark.parametrize("answer,value", [
    ({"type": "choice", "choice": {"label": "Tokyo"}}, "Tokyo"),
    ({"type": "choice", "choice": {"other": "Lima"}}, "Lima"),
    ({"type": "choices", "choices": {"labels": ["A"], "other": "B"}}, ["A", "B"]),
    ({"type": "boolean", "boolean": False}, False),
    ({"type": "number", "number": 0}, 0),
    ({"type": "date", "date": "2012-03-20T00:00:00Z"}, "2012-03-20T00:00:00Z"),
    ({"type": "payment", "payment": {"amount": "10", "success": True}},
     {"amount": "10", "success": True}),
    ({"type": "nouveau", "x": 1}, {"x": 1}),
])
def test_valeur_dune_reponse(answer, value):
    assert T.answer_value({"field": {"id": "q"}, **answer}) == value


# --- refus amont ---------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_un_4xx_devient_un_refus_nomme(banc, status):
    from oto.tools.common import UpstreamHTTPError

    def _leve(c):
        c.leve = UpstreamHTTPError(status, {"code": "X", "description": "d"},
                                   service="typeform")
    banc.prepare = _leve
    _refus(banc, "typeform_forms")


@pytest.mark.parametrize("status", [429, 503])
def test_429_et_5xx_restent_typés(banc, status):
    from oto.tools.common import UpstreamHTTPError

    def _leve(c):
        c.leve = UpstreamHTTPError(status, "busy", service="typeform")
    banc.prepare = _leve
    fn = {t.name: t for t in asyncio.run(banc.mcp._list_tools())}["typeform_forms"].fn
    with pytest.raises(UpstreamHTTPError) as e:
        fn()
    assert e.value.status_code == status

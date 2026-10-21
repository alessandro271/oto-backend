"""Tools `onenote` / `onenote_act` du connecteur OneNote (service du porteur `microsoft`).

Ce que ce fichier verrouille :
- la SURFACE : la paire de tout connecteur (17/10/2026), `onenote` en lecture, `onenote_act`
  qui agit ; le jeton du service `onenote` ;
- la RACINE : un client par lieu (la personne, `group_id`, `site_id`), jamais les deux ;
- le dispatch vers les VRAIES méthodes de `OneNoteClient` (autospec) ;
- une page lue en texte par défaut, en HTML avec ses ids sur demande ;
- l'écriture : un seul corps (markdown ou HTML), l'ajout en fin de page par défaut, les
  arguments d'une autre op refusés avant tout appel.
"""
import asyncio
from unittest.mock import create_autospec

import pytest
from oto_mcp.mcp_errors import McpError

pytestmark = pytest.mark.exige_pin_oto_core


@pytest.fixture
def client(monkeypatch):
    import oto.tools.microsoft as coeur

    inst = create_autospec(coeur.OneNoteClient, instance=True)
    construits = []
    monkeypatch.setattr(coeur, "OneNoteClient",
                        lambda jeton, **racine: construits.append((jeton, racine)) or inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")

    def jeton(sub, service):
        assert service == "onenote"
        return f"AT-{sub}"

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", jeton)
    inst.construits = construits
    return inst


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import onenote as X

    m = FastMCP("t")
    X.register(m)
    return asyncio.run(m.get_tool(name)).fn


PAGE = {"id": "p1", "title": "Réunion", "createdDateTime": "2026-10-20T08:00:00Z",
        "lastModifiedDateTime": "2026-10-21T08:00:00Z",
        "parentSection": {"id": "s1", "displayName": "CR"},
        "links": {"oneNoteWebUrl": {"href": "https://onenote/p1"}}}


def test_surface_lecture_et_acte():
    from fastmcp import FastMCP
    from oto_mcp.tools import onenote as X
    from oto_mcp.tools.lecture import ACTE, LECTURE

    m = FastMCP("t")
    X.register(m)
    outils = {t.name: t for t in asyncio.run(m.list_tools())}
    assert set(outils) == {"onenote", "onenote_act"}
    assert outils["onenote"].annotations == LECTURE
    assert outils["onenote_act"].annotations == ACTE


def test_carnets_de_la_personne_vue_resserree(client):
    client.list_notebooks.return_value = [
        {"id": "nb1", "displayName": "Perso", "isShared": False, "userRole": "Owner",
         "lastModifiedDateTime": "t", "links": {"oneNoteWebUrl": {"href": "https://n"}}}]
    out = _tool("onenote")()
    assert out == {"notebooks": [{"id": "nb1", "name": "Perso", "isShared": False,
                                  "userRole": "Owner", "lastModified": "t",
                                  "webUrl": "https://n"}], "count": 1}
    assert client.construits == [("AT-sub-1", {"group_id": None, "site_id": None})]
    client.list_notebooks.assert_called_once_with(limit=50)


def test_le_carnet_d_une_equipe_par_son_groupe(client):
    client.list_sections.return_value = []
    _tool("onenote")(op="sections", notebook_id="nb1", group_id="g-1")
    assert client.construits[0][1] == {"group_id": "g-1", "site_id": None}
    client.list_sections.assert_called_once_with(notebook_id="nb1", limit=50)


def test_groupe_et_site_ensemble_refuses_avant_le_jeton(client):
    with pytest.raises(McpError, match="not both"):
        _tool("onenote")(group_id="g", site_id="s")
    assert client.construits == []


def test_pages_d_une_section_filtrees_et_projetees(client):
    client.list_pages.return_value = [PAGE]
    out = _tool("onenote")(op="pages", section_id="s1", title="réu", fields=["id", "title"])
    client.list_pages.assert_called_once_with(section_id="s1", title_contains="réu", limit=50)
    assert out["pages"] == [{"id": "p1", "title": "Réunion"}]


def test_une_page_en_texte_par_defaut(client):
    client.get_page.return_value = PAGE
    client.get_page_content.return_value = (
        "<html><head><title>Réunion</title><style>p{x}</style></head><body>"
        "<div><p>Ordre   du jour</p><p>Décisions</p></div></body></html>")
    out = _tool("onenote")(op="page", page_id="p1")
    client.get_page_content.assert_called_once_with("p1", include_ids=False)
    assert out["text"] == "Réunion\nOrdre du jour\nDécisions"
    assert out["section"] == {"id": "s1", "name": "CR"} and "html" not in out


def test_une_page_en_html_avec_ses_ids(client):
    client.get_page.return_value = PAGE
    client.get_page_content.return_value = "<p id='p:{1}'>x</p>"
    out = _tool("onenote")(op="page", page_id="p1", format="html")
    client.get_page_content.assert_called_once_with("p1", include_ids=True)
    assert out["html"] == "<p id='p:{1}'>x</p>"


@pytest.mark.parametrize("kw, motif", [
    ({"op": "page"}, "requires `page_id`"),
    ({"op": "notebooks", "page_id": "p1"}, "does not use `page_id`"),
    ({"op": "pages", "format": "html"}, "`format` only applies"),
    ({"op": "page", "page_id": "p1", "fields": ["id"]}, "`fields` only applies"),
    ({"op": "delete"}, "op must be"),
])
def test_lecture_arguments_hors_op_refuses(client, kw, motif):
    with pytest.raises(McpError, match=motif):
        _tool("onenote")(**kw)


def test_creer_une_page_depuis_du_markdown(client):
    client.create_page.return_value = PAGE
    out = _tool("onenote_act")(op="create_page", section_id="s1", title="Réunion",
                               body="**Décisions**")
    args, kw = client.create_page.call_args
    assert args == ("s1",) and kw["title"] == "Réunion"
    assert "<strong>Décisions</strong>" in kw["body_html"]
    assert out["id"] == "p1" and out["webUrl"] == "https://onenote/p1"


def test_ajouter_en_fin_de_page_par_defaut(client):
    out = _tool("onenote_act")(op="update_page", page_id="p1", html="<p>Suite</p>")
    client.update_page.assert_called_once_with(
        "p1", [{"target": "body", "action": "append", "content": "<p>Suite</p>"}])
    assert out == {"id": "p1", "updated": True}


def test_inserer_avant_un_element(client):
    _tool("onenote_act")(op="update_page", page_id="p1", html="<p>Avant</p>",
                         target="#p:{1}", action="insert", position="before")
    client.update_page.assert_called_once_with(
        "p1", [{"target": "#p:{1}", "action": "insert", "content": "<p>Avant</p>",
                "position": "before"}])


def test_section_et_carnet(client):
    client.create_section.return_value = {"id": "s2", "displayName": "CR"}
    client.create_notebook.return_value = {"id": "nb2", "displayName": "Projets"}
    assert _tool("onenote_act")(op="create_section", notebook_id="nb1", name="CR")["id"] == "s2"
    assert _tool("onenote_act")(op="create_notebook", name="Projets",
                                site_id="site-1")["name"] == "Projets"
    client.create_section.assert_called_once_with("nb1", "CR")
    assert client.construits[-1][1] == {"group_id": None, "site_id": "site-1"}


@pytest.mark.parametrize("kw, motif", [
    ({"op": "create_page", "section_id": "s1", "title": "T"}, "exactly one of"),
    ({"op": "create_page", "section_id": "s1", "title": "T", "body": "a", "html": "b"},
     "exactly one of"),
    ({"op": "create_page", "section_id": "s1", "title": "T", "body": "a",
      "action": "replace"}, "only apply to op='update_page'"),
    ({"op": "update_page", "page_id": "p1", "body": "a", "position": "after"},
     "goes with action='insert'"),
    ({"op": "create_section", "name": "CR"}, "requires `notebook_id`"),
    ({"op": "create_notebook", "name": "N", "page_id": "p1"}, "does not use `page_id`"),
    ({"op": "delete_page", "page_id": "p1"}, "op must be"),
])
def test_ecriture_refusee_avant_tout_appel(client, kw, motif):
    with pytest.raises(McpError, match=motif):
        _tool("onenote_act")(**kw)
    assert client.construits == []

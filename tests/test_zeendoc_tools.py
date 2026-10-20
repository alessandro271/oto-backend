"""Connecteur Zeendoc — GED, lecture seule.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la paire lire/agir et la jointure au client oto-core. Ce fichier verrouille
ce qui est PROPRE à ce module :

- un seul outil `zeendoc` (LECTURE), le verbe de lecture dans `op` ;
- chaque op appelle la BONNE méthode, avec les bons arguments, et le credential à
  trois champs (login, mot de passe, armoire) arrive au client ;
- aucun argument ignoré en silence, les arguments requis nommés ;
- les vues par défaut (classeurs et champs, documents sans valeurs vides) ;
- le texte et le fichier rendus par `file_content.render_for_agent` ;
- la traduction des erreurs Zeendoc et la sonde de connexion.

Ne tournent qu'après installation d'oto-core avec `oto.tools.zeendoc`. Toutes les
valeurs sont factices.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

CREDS = {"login": "user@example.com", "password": "pw", "url_client": "cab_1"}

CONTEXT = {
    "client": {"status": [{"id": 1, "label": "INDEXED"}]},
    "collections": [
        {"collId": "coll_2", "label": "Suppliers", "rights": {"canSearchDocs": 1},
         "index": [
             {"indexId": "custom_t2", "label": "Supplier", "type": "TEXT"},
             {"indexId": "custom_n3", "label": "Type", "type": "DOCUMENT_TYPE",
              "possibleValue": [{"id": 3, "label": "Invoice", "extra": 1}]},
             {"indexId": "custom_n5", "label": "Folder", "type": "FOREIGN_TABLE",
              "possibleValue": [{"nomGroupe": "Invoice", "idFather": 3,
                                 "membreGroupe": [{"id": 7, "label": "Scans"}]},
                                {"nomGroupe": "Order", "idFather": 4}]},
             {"indexId": "custom_n30", "label": "Site", "type": "FOREIGN_TABLE",
              "isMultivalued": 1,
              "possibleValue": [{"id": i, "label": f"S{i}"} for i in range(40)]},
         ]},
        {"label": "_SERVICE_RECHERCHE_MULTI_CLASSEUR_LABEL", "index": [{}]},
    ],
}


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(CREDS))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import zeendoc

    m = FastMCP("t")
    zeendoc.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn():
    return _tools()["zeendoc"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.zeendoc.ZeendocClient") as cls:
        yield cls


@pytest.fixture()
def render(monkeypatch):
    seen = {}

    def fake(data, filename, mime, *, sub, prefix, **_):
        seen.update(data=data, filename=filename, mime=mime, sub=sub, prefix=prefix)
        return {"filename": filename, "encoding": "text"}

    monkeypatch.setattr("oto_mcp.file_content.render_for_agent", fake)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    return seen


# --- registre ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["zeendoc"]
    assert c.secret_kind == "fields"
    assert c.auth_modes == {"byo_user", "byo_org"}
    fields = {f.name: f.secret for f in c.credential_fields}
    assert fields == {"login": False, "password": True, "url_client": False}


def test_one_read_tool_served():
    tools = _tools()
    assert set(tools) == {"zeendoc"}
    assert tools["zeendoc"].annotations.readOnlyHint is True


# --- ops -> méthodes ----------------------------------------------------------

def test_credential_fields_reach_the_client(client):
    client.return_value.get_context.return_value = CONTEXT
    _fn()()
    client.assert_called_once_with(login="user@example.com", password="pw",
                                   url_client="cab_1")


def test_binders_view_keeps_binders_and_short_choices(client):
    client.return_value.get_context.return_value = CONTEXT
    out = _fn()(op="binders")
    assert out["statuses"] == [{"id": 1, "label": "INDEXED"}]
    [binder] = out["binders"]
    assert binder["binder"] == "coll_2" and binder["rights"] == {"canSearchDocs": 1}
    supplier, kind, folder, site = binder["fields"]
    assert supplier == {"field": "custom_t2", "label": "Supplier", "type": "TEXT"}
    assert kind["choices"] == [{"id": 3, "label": "Invoice"}]
    assert folder["choices"] == [{"id": 7, "label": "Scans", "group": "Invoice"}]
    assert site["choices_count"] == 40 and site["multivalued"] is True
    assert _fn()(op="binders", full=True) == CONTEXT


def test_search_maps_arguments_and_trims_the_view(client):
    client.return_value.search_documents.return_value = {
        "result": 0, "nbDocs": 1,
        "stats": {"custom_f1": {"min": 1, "max": 1, "sum": 1, "count": 1, "stddev": None}},
        "document": [{"resId": 9, "properties": {"filename": "F1", "creationDateUtc": "x"},
                      "indexes": {"custom_t2": ["ACME"], "custom_t5": []},
                      "displayableIndexes": {"custom_t2": ["ACME"], "custom_t5": []}}]}
    crit = [{"field": "custom_t2", "operator": "EQUALS", "value": "ACME"}]
    out = _fn()(op="search", binder="coll_2", criteria=crit, order_by="custom_f1",
                limit=10)
    client.return_value.search_documents.assert_called_once_with(
        "coll_2", crit, match="all", order_by="custom_f1", order="desc", offset=0,
        limit=10, columns=None)
    assert out == {"nbDocs": 1,
                   "stats": {"custom_f1": {"sum": 1, "count": 1, "min": 1, "max": 1}},
                   "document": [{"resId": 9, "properties": {"filename": "F1"},
                                 "indexes": {"custom_t2": ["ACME"]},
                                 "displayableIndexes": {"custom_t2": ["ACME"]}}]}


def test_count(client):
    _fn()(op="count", binder="coll_2", match="any")
    client.return_value.count_documents.assert_called_once_with("coll_2", None, match="any")


def test_document(client):
    client.return_value.get_document.return_value = {"resId": 9, "indexes": {"a": []}}
    assert _fn()(op="document", binder="coll_2", res_id=9)["indexes"] == {}
    client.return_value.get_document.assert_called_once_with("coll_2", 9, columns=None)


def test_field_ops(client):
    _fn()(op="choices", binder="coll_2", field="custom_n30")
    _fn()(op="values", binder="coll_2", field="custom_t2", term="ac")
    client.return_value.list_field_choices.assert_called_once_with("coll_2", "custom_n30")
    client.return_value.suggest_field_values.assert_called_once_with(
        "coll_2", "custom_t2", "ac")


def test_text_goes_through_render_for_agent(client, render):
    client.return_value.get_document_text.return_value = "Invoice — total"
    _fn()(op="text", binder="coll_2", res_id=9)
    assert render == {"data": "Invoice — total".encode("utf-8"),
                      "filename": "coll_2_9.txt", "mime": "text/plain; charset=utf-8",
                      "sub": "sub-1", "prefix": "zeendoc-files"}


def test_file_goes_through_render_for_agent(client, render):
    client.return_value.download_document.return_value = {
        "filename": "F1.pdf", "content_type": "application/pdf", "content": b"%PDF-1.4"}
    _fn()(op="file", binder="coll_2", res_id=9, version="original")
    client.return_value.download_document.assert_called_once_with(
        "coll_2", 9, version="original")
    assert (render["data"], render["filename"], render["mime"]) == (
        b"%PDF-1.4", "F1.pdf", "application/pdf")


def test_storage_unavailable_is_a_named_refusal(client, monkeypatch):
    from oto_mcp import file_content

    def unavailable(*a, **k):
        raise file_content.MediaUnavailable("storage not configured")

    client.return_value.download_document.return_value = {
        "filename": "F1.pdf", "content_type": "application/pdf", "content": b"%PDF"}
    monkeypatch.setattr("oto_mcp.file_content.render_for_agent", unavailable)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    with pytest.raises(McpError, match="storage not configured"):
        _fn()(op="file", binder="coll_2", res_id=9)


# --- refus locaux -------------------------------------------------------------

@pytest.mark.parametrize("kwargs, name", [
    ({"op": "search"}, "binder"),
    ({"op": "document", "binder": "coll_2"}, "res_id"),
    ({"op": "values", "binder": "coll_2", "field": "custom_t2"}, "term"),
    ({"op": "choices", "binder": "coll_2"}, "field"),
])
def test_required_argument_is_named(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "binders", "binder": "coll_2"}, "binder"),
    ({"op": "count", "binder": "coll_2", "limit": 5}, "limit"),
    ({"op": "text", "binder": "coll_2", "res_id": 9, "full": True}, "full"),
    ({"op": "file", "binder": "coll_2", "res_id": 9, "columns": ["a"]}, "columns"),
])
def test_ignored_argument_is_refused(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


def test_client_value_error_becomes_a_named_refusal(client):
    client.return_value.count_documents.side_effect = ValueError(
        "`criteria[1].operator` invalid: 'BEGIN WITH'.")
    with pytest.raises(McpError, match="BEGIN WITH"):
        _fn()(op="count", binder="coll_2", criteria=[{"field": "a", "operator": "BEGIN WITH"}])


# --- erreurs amont, sonde -------------------------------------------------------

@pytest.mark.parametrize("status, needle", [
    (401, "login"), (403, "no right"), (404, "not found"), (429, "retry later"),
    (503, "HTTP 503")])
def test_upstream_errors_are_translated(client, status, needle):
    from oto.tools.common.errors import UpstreamHTTPError

    client.return_value.get_context.side_effect = UpstreamHTTPError(
        status, {"error": "x"}, service="zeendoc")
    with pytest.raises(McpError, match=needle):
        _fn()(op="binders")


def test_verify_probe(client):
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import zeendoc

    zeendoc._verify(dict(CREDS))
    client.return_value.probe.assert_called_once_with()
    client.return_value.probe.side_effect = UpstreamHTTPError(401, {"error": "x"})
    with pytest.raises(connector_verify.NonAutorise):
        zeendoc._verify(dict(CREDS))

"""Connecteur Claap — enregistrements de réunion (câblé 2026-09-29).

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie et la jointure au client oto-core. Ce fichier verrouille ce qui est PROPRE
à ce module :

- la **vue resserrée** de `op="list"` (aucune URL signée ; `full=True` rend le brut) ;
- le transcript **texte par défaut**, rendu dans un objet (le client rend une `str`) ;
- le **dry_run de suppression**, qui lit sans supprimer ;
- les **refus d'arguments ignorés**, sur chaque op sans exception ;
- la traduction des erreurs Claap (`path` d'un `validation_error`, 404 « privé »,
  enregistrement créé mais transcript refusé) ;
- la sonde de connexion sur `/workspaces/mine`.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("cla_k", False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import claap

    m = FastMCP("t")
    claap.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn(name):
    return _tools()[name].fn


REC = {
    "id": "rec_1", "title": "Démo", "createdAt": "2026-09-01T10:00:00Z",
    "durationSeconds": 1800, "state": "Ready", "source": "Zoom",
    "url": "https://app.claap.io/x", "labels": [], "thumbnailUrl": "https://signed/t",
    "video": {"url": "https://signed/v"},
    "transcripts": [{"langIso2": "fr", "url": "https://signed/j",
                     "textUrl": "https://signed/t"}],
    "workspace": {"id": "w", "name": "W"},
    "recorder": {"id": "u", "name": "Ana", "email": "ana@x.co", "attended": True},
    "meeting": {"type": "external", "startingAt": "2026-09-01T10:00:00Z",
                "endingAt": "2026-09-01T10:30:00Z",
                "participants": [{"name": "Bob", "email": "bob@y.co", "attended": True}]},
}


@pytest.fixture()
def client():
    with patch("oto.tools.claap.client.ClaapClient") as cls:
        yield cls.return_value


# --- registre ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["claap"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}


def test_tools_served_with_descriptions():
    tools = _tools()
    assert set(tools) == {"claap_recordings", "claap_recording_views"}
    assert all(t.description for t in tools.values())


def test_probe_hits_workspace(client):
    from oto_mcp.tools.claap import _verify
    _verify({"key": "cla_k"})
    client.probe.assert_called_once_with()


# --- list -------------------------------------------------------------------

def test_list_is_slim_by_default(client):
    client.list_recordings.return_value = {"result": {
        "recordings": [REC], "pagination": {"nextCursor": "c2", "totalCount": 9}}}
    out = _fn("claap_recordings")(op="list", recorder_email="ana@x.co")
    row = out["result"]["recordings"][0]
    assert out["result"]["pagination"]["nextCursor"] == "c2"
    assert row["id"] == "rec_1" and row["recorder"] == {"name": "Ana", "email": "ana@x.co"}
    assert row["transcriptLanguages"] == ["fr"]
    flat = repr(row)
    assert "signed" not in flat and "workspace" not in row
    assert client.list_recordings.call_args.kwargs["recorder_email"] == "ana@x.co"


def test_list_full_is_raw(client):
    raw = {"result": {"recordings": [REC], "pagination": {"totalCount": 1}}}
    client.list_recordings.return_value = raw
    assert _fn("claap_recordings")(op="list", full=True) == raw


@pytest.mark.parametrize("kw", [{"recording_id": "r"}, {"author_email": "a@b.co"},
                                {"format": "json"}, {"dry_run": True}])
def test_list_refuses_foreign_args(client, kw):
    with pytest.raises(McpError):
        _fn("claap_recordings")(op="list", **kw)
    client.list_recordings.assert_not_called()


def test_client_value_error_becomes_tool_error(client):
    client.list_recordings.side_effect = ValueError("`labels` is only accepted … channel_id")
    with pytest.raises(McpError, match="channel_id"):
        _fn("claap_recordings")(op="list", labels=["x"])


# --- get / transcript / delete ----------------------------------------------

def test_get_requires_id(client):
    with pytest.raises(McpError, match="recording_id"):
        _fn("claap_recordings")(op="get")


@pytest.mark.parametrize("op", ["get", "transcript", "delete"])
@pytest.mark.parametrize("kw", [{"recorder_email": "a@b.co"}, {"channel_id": "c"},
                                {"cursor": "c"}, {"video_url": "u"}])
def test_single_record_ops_refuse_list_and_create_args(client, op, kw):
    with pytest.raises(McpError):
        _fn("claap_recordings")(op=op, recording_id="r", **kw)


def test_get_refuses_format_and_dry_run(client):
    for kw in ({"format": "text"}, {"dry_run": True}):
        with pytest.raises(McpError):
            _fn("claap_recordings")(op="get", recording_id="r", **kw)


def test_transcript_defaults_to_text_wrapped(client):
    client.get_recording_transcript.return_value = "00:01 speaker_1: Bonjour"
    out = _fn("claap_recordings")(op="transcript", recording_id="rec_1")
    client.get_recording_transcript.assert_called_once_with(
        "rec_1", lang=None, format="text")
    assert out == {"recording_id": "rec_1", "format": "text", "lang": None,
                   "transcript": "00:01 speaker_1: Bonjour"}


def test_transcript_json_passthrough(client):
    client.get_recording_transcript.return_value = {"result": {"transcript": {}}}
    out = _fn("claap_recordings")(op="transcript", recording_id="r", format="json")
    assert out == {"result": {"transcript": {}}}


def test_delete_dry_run_reads_never_deletes(client):
    client.get_recording.return_value = {"result": {"recording": REC}}
    out = _fn("claap_recordings")(op="delete", recording_id="rec_1", dry_run=True)
    assert out["dry_run"] is True
    assert out["would_delete"]["title"] == "Démo"
    assert out["would_delete"]["recorder"] == "ana@x.co"
    client.delete_recording.assert_not_called()


def test_delete_for_real(client):
    client.delete_recording.return_value = {"result": {"ok": True}}
    assert _fn("claap_recordings")(op="delete", recording_id="r") == {"result": {"ok": True}}


# --- create -----------------------------------------------------------------

def test_create_requires_author(client):
    with pytest.raises(McpError, match="author_email"):
        _fn("claap_recordings")(op="create", video_url="u")


def test_create_passes_everything(client):
    tr = {"segments": [{"start": 0, "end": 1, "speakerId": "1", "text": "x"}]}
    _fn("claap_recordings")(op="create", author_email="a@b.co", title="T",
                            channel_id="ch", transcript=tr)
    client.create_recording.assert_called_once_with(
        "a@b.co", title="T", channel_id="ch", source=None, meeting=None,
        deal=None, video_url=None, transcript=tr)


@pytest.mark.parametrize("kw", [{"recording_id": "r"}, {"recorder_email": "x"},
                                {"labels": ["a"]}, {"format": "text"},
                                {"dry_run": True}, {"full": True}])
def test_create_refuses_foreign_args(client, kw):
    with pytest.raises(McpError):
        _fn("claap_recordings")(op="create", author_email="a@b.co",
                                video_url="u", **kw)
    client.create_recording.assert_not_called()


# --- erreurs ----------------------------------------------------------------

def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body, service="claap")


def test_validation_error_names_the_field(client):
    client.list_recordings.side_effect = _upstream(400, {"error": {
        "type": "validation_error", "message": "Value is not a valid integer",
        "path": "limit"}})
    with pytest.raises(McpError, match="field `limit`"):
        _fn("claap_recordings")(op="list")


def test_404_mentions_private(client):
    client.get_recording.side_effect = _upstream(404, {"error": {
        "type": "not_found", "message": "Recording was not found"}})
    with pytest.raises(McpError, match="private"):
        _fn("claap_recordings")(op="get", recording_id="r")


def test_404_on_view_does_not_talk_about_recordings(client):
    """Constaté en live : un 404 de vue disait « enregistrement privé »."""
    client.get_recording_view.side_effect = _upstream(404, {"error": {
        "type": "not_found", "message": "Recording preset not found"}})
    with pytest.raises(McpError) as ei:
        _fn("claap_recording_views")(op="get", view_id="nope")
    assert "PUBLIC views" in str(ei.value) and "private recording" not in str(ei.value)


def test_404_on_translation_points_to_available_languages(client):
    """Constaté en live : `lang` sans traduction → 404 « Transcript could not be found »."""
    client.get_recording_transcript.side_effect = _upstream(404, {"error": {
        "type": "not_found", "message": "Transcript could not be found"}})
    with pytest.raises(McpError, match="langIso2"):
        _fn("claap_recordings")(op="transcript", recording_id="r", lang="fr")


def test_quota_403_is_named(client):
    """Constaté en live sur un plan d'essai : 403 « Recording quota exceeded »."""
    client.create_recording.side_effect = _upstream(403, {"error": {
        "type": "forbidden", "message": "Recording quota exceeded"}})
    with pytest.raises(McpError, match="plan quota"):
        _fn("claap_recordings")(op="create", author_email="a@b.co", video_url="u")


def test_half_created_recording_is_named(client):
    client.create_recording.side_effect = _upstream(403, {
        "recording_id": "rec_9", "error": "PUT du transcript refusé",
        "body": "SignatureDoesNotMatch"})
    with pytest.raises(McpError, match="rec_9"):
        _fn("claap_recordings")(op="create", author_email="a@b.co",
                                transcript={"segments": []})


# --- vues -------------------------------------------------------------------

def test_views_list_and_get(client):
    _fn("claap_recording_views")()
    _fn("claap_recording_views")(op="get", view_id="v1")
    client.list_recording_views.assert_called_once_with()
    client.get_recording_view.assert_called_once_with("v1")


def test_views_list_refuses_view_id(client):
    with pytest.raises(McpError):
        _fn("claap_recording_views")(op="list", view_id="v1")


def test_views_get_requires_id(client):
    with pytest.raises(McpError, match="view_id"):
        _fn("claap_recording_views")(op="get")

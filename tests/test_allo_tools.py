"""Tools `allo` / `allo_act` and the Allo probe.

What this file locks — the generic tripwires already cover registry, publisher,
logo, module derivation, served prose, the `<ns>`/`<ns>_act` convention and the
join to the client:
- the SURFACE (the pair) and the routing to the right client method, with the
  API key passed as is; the fake client is specced on the library's `AlloClient`,
  so a method it no longer has (`get_recording_link`) fails the call;
- NO RECORDING ADDRESS REACHES THE AGENT, judged on what the tools RETURN: every
  read op, list or detail, `full` or not, and every act op, fed answers that carry
  Allo's signed links at every place a call sits, returns none of them; the audio
  goes through `get_call_recording` and comes back as a transit reference; no
  client method that returns a link is called anywhere in the module;
- the tightened view of lists (named keys dropped, `pagination` untouched,
  `projection` naming them) and `full=True` for the raw records;
- intent refusals (an argument of another op, a missing required argument, a
  CRM field the record does not take, a text with none or two senders, or too
  long) — nothing is called;
- the translation of upstream refusals, Allo's own code and suggestion kept;
- the probe: 401/403 → `NonAutorise`, any other code unclassified.
"""
import ast
import asyncio
import json
import pathlib
from unittest.mock import MagicMock, create_autospec

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    """The fake client, specced on the library's: a method it lacks raises."""
    from oto.tools.allo import AlloClient
    inst, built = create_autospec(AlloClient, instance=True), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.allo.AlloClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("ak_test", False))
    inst.built = built
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import allo as A

    m = FastMCP("t")
    A.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"error": {"type": "x"}}, service="allo")


# --- surface ------------------------------------------------------------------

def test_surface_is_the_pair(client):
    tools = {t.name: t for t in asyncio.run(_mcp().list_tools())}
    assert set(tools) == {"allo", "allo_act"}
    assert tools["allo"].annotations.readOnlyHint is True
    assert tools["allo_act"].annotations.readOnlyHint is False
    assert all(t.description for t in tools.values())


def test_registry_declares_one_api_key():
    from oto_mcp import providers
    c = providers.REGISTRY["allo"]
    assert c.kind == "tools" and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})


def test_the_key_is_passed_as_is(client):
    _tool("allo")(op="me")
    assert client.built == [{"api_key": "ak_test"}]


# --- reads ----------------------------------------------------------------------

def test_search_maps_dates_type_and_transcript(client):
    _tool("allo")(op="search", search="pricing", item_type="CALL", date_from="2026-10-01",
                  date_to="2026-10-20", transcript=True, size=10)
    client.search_conversation_items.assert_called_once_with(
        allo_number=None, contact_number=None, user_id=None, direction=None, type="CALL",
        result=None, tags=None, unread=None, unresponded=None, search="pricing",
        date={"from": "2026-10-01", "to": "2026-10-20"}, sort=None, extend="transcript",
        page=None, size=10)


def test_notes_of_a_person_or_of_a_conversation(client):
    _tool("allo")(op="notes", person_id="per-1")
    client.list_person_notes.assert_called_once_with("per-1", page=None, size=None)
    _tool("allo")(op="notes", contact_number="+33199000001", allo_number="+33199000000")
    client.list_conversation_notes.assert_called_once_with(
        "+33199000001", "+33199000000", page=None, size=None)


def test_attributes_by_entity(client):
    _tool("allo")(op="attributes", entity="company")
    client.list_company_attributes.assert_called_once_with(page=None, size=None)
    client.list_person_attributes.assert_not_called()


def test_outbound_lists_a_stage_s_calls(client):
    _tool("allo")(op="outbound", date_from="2026-10-01", date_to="2026-10-20",
                  tags=["meeting_booked"], stage="CONVERSION", page=2)
    client.get_outbound_metrics.assert_called_once_with(
        {"from": "2026-10-01", "to": "2026-10-20"}, compare_date=None, user_ids=None,
        allo_numbers=None, tags=["meeting_booked"], granularity=None, extend="items",
        stage="CONVERSION", page=2, size=None)


def test_metrics_with_a_comparison(client):
    _tool("allo")(op="metrics", date_from="2026-10-12", date_to="2026-10-18",
                  compare_from="2026-10-05", compare_to="2026-10-11")
    client.get_team_metrics.assert_called_once_with(
        {"from": "2026-10-12", "to": "2026-10-18"},
        compare_date={"from": "2026-10-05", "to": "2026-10-11"}, user_ids=None,
        allo_numbers=None)


@pytest.mark.parametrize("kwargs,needle", [
    ({"op": "item"}, "`item_id` is required"),
    ({"op": "conversations"}, "`allo_number` is required"),
    ({"op": "person", "person_id": "per-1", "search": "x"}, "does not take `search`"),
    ({"op": "me", "transcript": True}, "does not take `transcript`"),
    ({"op": "notes"}, "needs `person_id`"),
    ({"op": "notes", "person_id": "per-1", "contact_number": "+3361"}, "not both"),
    ({"op": "search", "date_from": "2026-10-01"}, "go together"),
    ({"op": "outbound", "date_from": "2026-10-01", "date_to": "2026-10-02", "page": 2},
     "give `stage`"),
])
def test_read_refusals(client, kwargs, needle):
    with pytest.raises(McpError, match=needle):
        _tool("allo")(**kwargs)
    assert client.built == []


# --- lists: tightened by default, raw on `full` ------------------------------------

_PAGE = {"page": 1, "size": 20, "total_count": 1, "total_pages": 1, "has_more": False}


def test_a_list_drops_the_ai_blobs_and_names_them(client):
    client.search_conversation_items.return_value = {"data": [{
        "id": "cll-1", "summary": "Pricing call.", "extracted_data": {"budget": "10k"},
        "data_collected": {"x": 1}}], "pagination": _PAGE}
    out = _tool("allo")(op="search", search="pricing")
    assert out["data"] == [{"id": "cll-1", "summary": "Pricing call."}]
    assert out["pagination"] == _PAGE
    assert out["projection"]["omitted"] == ["data_collected", "extracted_data"]


def test_full_returns_the_raw_list(client):
    page = {"data": [{"id": "per-1", "interactions": [{"type": "CALL"}]}], "pagination": _PAGE}
    client.search_people.return_value = page
    assert _tool("allo")(op="people", full=True) == page


def test_a_detail_is_not_tightened_and_refuses_full(client):
    client.get_conversation_item.return_value = {"data": {"id": "cll-1",
                                                          "extracted_data": {"a": 1}}}
    assert _tool("allo")(op="item", item_id="cll-1") == {
        "data": {"id": "cll-1", "extracted_data": {"a": 1}}}
    with pytest.raises(McpError, match="does not take `full`"):
        _tool("allo")(op="item", item_id="cll-1", full=True)


# --- the audio of a call: a transit reference, never a link -------------------------

@pytest.fixture
def audio(monkeypatch):
    """The deposit in transit, observed."""
    vu = {"deposer": []}

    def deposer(prefix, data, mime, filename, *, lien=False):
        vu["deposer"].append((prefix, data, mime, filename, lien))
        return {"source": {"kind": "transit", "ref": prefix + "/" + "0" * 32},
                "filename": filename, "mimeType": mime, "size": len(data)}

    monkeypatch.setattr("oto_mcp.transit.deposer", deposer)
    return vu


def test_the_recording_goes_into_transit(client, audio):
    from oto.connectors.files import FileOut
    client.get_call_recording.return_value = FileOut(
        "call-cll-1-recording.mp3", b"ID3audio", "audio/mpeg")
    out = _tool("allo")(op="recording", item_id="cll-1")
    client.get_call_recording.assert_called_once_with("cll-1")
    assert audio["deposer"] == [("allo-recordings", b"ID3audio", "audio/mpeg",
                                 "call-cll-1-recording.mp3", False)]
    assert out == {"source": {"kind": "transit", "ref": "allo-recordings/" + "0" * 32},
                   "filename": "call-cll-1-recording.mp3", "mimeType": "audio/mpeg",
                   "size": 8}


def test_the_transit_space_is_declared():
    from oto_mcp import media_store
    assert "allo-recordings" in media_store.PREFIXES_TRANSIT


@pytest.mark.parametrize("code", ["file_missing", "file_too_large",
                                  "file_download_refused", "file_type_refused"])
def test_a_file_refused_by_the_library_is_named(client, audio, code):
    from oto.connectors.files import FileOutputError
    client.get_call_recording.side_effect = FileOutputError(code, "refus de test")
    with pytest.raises(McpError, match=f"Allo recording: {code}: refus de test"):
        _tool("allo")(op="recording", item_id="cll-1")
    assert audio["deposer"] == []


def test_a_slow_recording_is_a_tool_error(client, audio):
    import requests
    client.get_call_recording.side_effect = requests.Timeout("lent")
    with pytest.raises(McpError, match="Allo recording: .*120 s"):
        _tool("allo")(op="recording", item_id="cll-1")
    assert audio["deposer"] == []


def test_a_transit_failure_is_a_tool_error(client, monkeypatch):
    from oto.connectors.files import FileOut
    from oto_mcp import transit

    def refuse(*a, **kw):
        raise transit.TransitError("the file could not be put in transit: storage error (x).")

    monkeypatch.setattr("oto_mcp.transit.deposer", refuse)
    client.get_call_recording.return_value = FileOut("a.mp3", b"ID3", "audio/mpeg")
    with pytest.raises(McpError, match="Allo recording: the file could not be put in transit"):
        _tool("allo")(op="recording", item_id="cll-1")


@pytest.mark.parametrize("status,code,needle", [
    (404, "RECORDING_NOT_FOUND", "has no recording"),
    (403, "RECORDING_ACCESS_DENIED", "may not play"),
    (403, "RECORDING_DOWNLOAD_RESTRICTED", "HIPAA"),
])
def test_allo_s_recording_refusals_are_said(client, audio, status, code, needle):
    client.get_call_recording.side_effect = _upstream(status, {"error": {"code": code}})
    with pytest.raises(McpError, match=needle):
        _tool("allo")(op="recording", item_id="cll-1")
    assert audio["deposer"] == []


# --- GUARD: no recording address reaches the agent ----------------------------------
#
# Judged on what the tools RETURN, not on a file name: Allo's real shape
# (OpenAPI `ConversationItem`) puts `recording_url` and the signed
# `recording_download_url` (48 hours, plays without a login) with its expiry on every
# call, and the legacy `CallApiResponse` a `recording_url`. Every client method is made
# to answer them at every place a call sits — `data` as an object or a list,
# `last_item` of a conversation, `items` of a conversation, `call` of a legacy
# result — so the guard holds whatever the client strips or not.

_SIGNED = "https://files.example.test/sign/recordings/cll-1.mp3?token=SECRET-SIGNATURE"
_ADDRESSES = {"recording_url": "https://files.example.test/recordings/cll-1.mp3",
              "recording_download_url": _SIGNED,
              "recording_download_expires_at": "2026-01-01T00:00:00Z"}


def _call():
    return {"id": "cll-1", "type": "CALL", "duration": 42, "recording_duration": 30,
            "summary": "Pricing call.", **_ADDRESSES}


def _poisoned():
    return {"data": [_call(), {"contact_number": "+33199000001", "last_item": _call(),
                               "items": [_call()]}],
            "results": [{"type": "CALL", "call": _call()}],
            "detail": {"data": _call()}, "pagination": dict(_PAGE)}


def _poison(client) -> None:
    from oto.tools.allo.client import AlloClient  # the real class: the package's is patched
    for name in dir(AlloClient):
        if not name.startswith("_") and callable(getattr(AlloClient, name)):
            getattr(client, name).side_effect = lambda *a, **kw: _poisoned()


def _assert_no_address(out) -> None:
    text = json.dumps(out)  # a fake answer (not JSON) would pass vacuously: it raises
    assert '"cll-1"' in text, text  # the poisoned answer did go through
    assert "token=" not in text and "files.example.test" not in text, text
    for key in _ADDRESSES:
        assert f'"{key}"' not in text, key


#: One valid call per read op of `allo` (`recording` apart: it returns a file).
_READS = {
    "me": {}, "users": {}, "numbers": {}, "tags": {},
    "conversations": {"allo_number": "+33199000000"},
    "search": {"search": "pricing"},
    "item": {"item_id": "cll-1"}, "items": {"ids": ["cll-1"]},
    "notes": {"person_id": "per-1"},
    "people": {}, "person": {"person_id": "per-1"},
    "companies": {}, "company": {"company_id": "com-1"},
    "deals": {}, "deal": {"deal_id": "dea-1"},
    "attributes": {"entity": "person"}, "queue": {},
    "metrics": {"date_from": "2026-10-01", "date_to": "2026-10-20"},
    "outbound": {"date_from": "2026-10-01", "date_to": "2026-10-20", "stage": "CONVERSION"},
}


def test_every_read_op_is_judged():
    from oto_mcp.tools import allo as A
    assert set(_READS) | {"recording"} == set(A._READ_OPS)


@pytest.mark.parametrize("op", sorted(_READS))
def test_no_read_returns_a_recording_address(client, op):
    from oto_mcp.tools import allo as A
    _poison(client)
    _assert_no_address(_tool("allo")(op=op, **_READS[op]))
    if op in A._LIST_OPS:
        out = _tool("allo")(op=op, full=True, **_READS[op])
        _assert_no_address(out)
        # `full` keeps everything else of the call: only the addresses are gone.
        assert out["data"][0] == {k: v for k, v in _call().items() if k not in _ADDRESSES}


#: One valid call per act op of `allo_act`.
_ACTS = {
    "send_sms": {"to": "+33199000001", "message": "x", "sender_id": "Acme"},
    "update_summary": {"item_id": "cll-1", "content": "x"},
    "update_conversation": {"contact_number": "+33199000001", "action": "READ"},
    "create_tag": {"name": "Hot"}, "add_tags": {"item_id": "cll-1", "tags": ["hot"]},
    "remove_tag": {"item_id": "cll-1", "tag": "hot"},
    "create_note": {"content": "x", "person_id": "per-1"},
    "create_person": {"fields": {"numbers": ["+33199000001"]}},
    "update_person": {"person_id": "per-1", "fields": {"name": "M"}},
    "create_company": {"fields": {"name": "Acme"}},
    "update_company": {"company_id": "com-1", "fields": {"website": "x"}},
    "create_queue": {"name": "October"}, "update_queue": {"name": "October"},
    "add_queue_numbers": {"entries": [{"number": "+33199000001"}]},
    "remove_queue_numbers": {"unassigned": True},
}


@pytest.mark.parametrize("op", sorted(_ACTS))
def test_no_act_returns_a_recording_address(client, op):
    from oto_mcp.tools import allo as A
    assert set(_ACTS) == set(A._ACT_OPS)
    _poison(client)
    _assert_no_address(_tool("allo_act")(op=op, **_ACTS[op]))


def test_the_recording_op_returns_a_reference_not_a_link(client, audio):
    from oto.connectors.files import FileOut
    _poison(client)
    client.get_call_recording.side_effect = None
    client.get_call_recording.return_value = FileOut("a.mp3", b"ID3", "audio/mpeg")
    out = _tool("allo")(op="recording", item_id="cll-1")
    assert set(out) == {"source", "filename", "mimeType", "size"}
    assert out["source"]["kind"] == "transit"
    assert "http" not in json.dumps(out)
    assert [d[-1] for d in audio["deposer"]] == [False]  # never `lien=True`


def test_the_library_s_addresses_are_all_removed_here():
    """The client's list may grow: each address it names must be one this module removes."""
    from oto.tools.allo.client import RECORDING_ADDRESS_FIELDS
    from oto_mcp.tools import allo as A
    assert set(RECORDING_ADDRESS_FIELDS) <= A._RECORDING_ADDRESSES
    assert set(_ADDRESSES) <= A._RECORDING_ADDRESSES


#: The client methods the module may call. A method that returns a recording LINK
#: (`get_recording_link`, Allo 1.0.0) is not one: adding a method here is a review.
_ALLOWED_METHODS = frozenset({
    "get_me", "probe", "list_users", "list_numbers", "list_conversations",
    "search_conversation_items", "get_conversation_item", "get_conversation_items",
    "get_call_recording", "list_tags", "list_person_notes", "list_conversation_notes",
    "search_people", "get_person", "search_companies", "get_company", "search_deals",
    "get_deal", "list_person_attributes", "list_company_attributes", "get_dialing_queue",
    "get_team_metrics", "get_outbound_metrics", "send_sms", "update_call_summary",
    "update_conversation", "create_tag", "add_item_tags", "remove_item_tag",
    "create_person_note", "create_conversation_note", "create_person", "update_person",
    "create_company", "update_company", "reset_dialing_queue", "update_dialing_queue",
    "add_queue_numbers", "remove_queue_numbers",
})


def _client_methods_called() -> set[str]:
    """`X` of every `_client().X(…)` / `AlloClient(…).X(…)` in the module, read by AST."""
    from oto_mcp.tools import allo as A
    tree = ast.parse(pathlib.Path(A.__file__).read_text(encoding="utf-8"))
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Call)
            and getattr(n.func.value.func, "id", None) in ("_client", "AlloClient")}


def test_no_client_method_that_returns_a_link_is_called():
    called = _client_methods_called()
    assert "get_call_recording" in called and "get_recording_link" not in called
    assert called <= _ALLOWED_METHODS, sorted(called - _ALLOWED_METHODS)
    from oto_mcp.tools import allo as A
    assert "get_recording_link" not in pathlib.Path(A.__file__).read_text(encoding="utf-8")


# --- writes ---------------------------------------------------------------------

def test_send_sms_from_a_sender_id(client):
    _tool("allo_act")(op="send_sms", to="+33199000001", message="Bonjour", sender_id="Acme")
    client.send_sms.assert_called_once_with("+33199000001", "Bonjour", sender_id="Acme")


def test_send_sms_from_a_team_number(client):
    _tool("allo_act")(op="send_sms", to="+14155550123", message="Hi", from_number="+14155550100")
    client.send_sms.assert_called_once_with("+14155550123", "Hi", from_number="+14155550100")


@pytest.mark.parametrize("senders,needle", [
    ({}, "not none"),
    ({"from_number": "", "sender_id": ""}, "not none"),
    ({"from_number": "+14155550100", "sender_id": "Acme"}, "not both"),
])
def test_send_sms_takes_one_sender(client, senders, needle):
    with pytest.raises(McpError, match=f"takes ONE sender.*{needle}"):
        _tool("allo_act")(op="send_sms", to="+33199000001", message="x", **senders)
    client.send_sms.assert_not_called()


def test_send_sms_an_empty_sender_is_none(client):
    _tool("allo_act")(op="send_sms", to="+33199000001", message="x", from_number="",
                      sender_id="Acme")
    client.send_sms.assert_called_once_with("+33199000001", "x", sender_id="Acme")


def test_send_sms_one_sender_in_the_schema():
    props = asyncio.run(_mcp().get_tool("allo_act")).parameters["properties"]
    assert {"from_number", "sender_id"} <= set(props)
    assert "never both" in props["from_number"]["description"]
    assert "never both" in props["sender_id"]["description"]


def test_send_sms_refuses_a_text_over_1000_characters(client):
    with pytest.raises(McpError, match="at most 1000 characters"):
        _tool("allo_act")(op="send_sms", to="+33199000001", message="x" * 1001,
                          sender_id="Acme")
    client.send_sms.assert_not_called()


def test_create_person_takes_numbers_out_of_fields(client):
    _tool("allo_act")(op="create_person", fields={"numbers": ["+33199000001"], "name": "Marie"})
    client.create_person.assert_called_once_with(["+33199000001"], name="Marie")


def test_create_company_takes_its_name_out_of_fields(client):
    _tool("allo_act")(op="create_company", fields={"name": "Acme", "industry": "Software"})
    client.create_company.assert_called_once_with("Acme", industry="Software")


def test_create_note_on_a_conversation(client):
    _tool("allo_act")(op="create_note", content="Call back Friday",
                      contact_number="+33199000001")
    client.create_conversation_note.assert_called_once_with(
        "+33199000001", "Call back Friday", allo_number=None, sender_id=None)


def test_queue_ops_route_to_the_dialing_queue(client):
    _tool("allo_act")(op="create_queue", name="October", email="rep@example.test")
    client.reset_dialing_queue.assert_called_once_with(name="October", user_id=None,
                                                       email="rep@example.test")
    entries = [{"number": "+33199000001", "name": "Marie"}]
    _tool("allo_act")(op="add_queue_numbers", entries=entries)
    client.add_queue_numbers.assert_called_once_with(entries, user_id=None, email=None)
    _tool("allo_act")(op="remove_queue_numbers", unassigned=True)
    client.remove_queue_numbers.assert_called_once_with(
        number=None, position=None, unassigned=True, user_id=None, email=None)


@pytest.mark.parametrize("kwargs,needle", [
    ({"op": "create_person", "fields": {"name": "Marie"}}, "fields.numbers"),
    ({"op": "create_person", "fields": {"numbers": ["+1"], "phone": "x"}}, "does not take `phone`"),
    ({"op": "update_company", "company_id": "com-1", "fields": {"numbers": ["+1"]}},
     "does not take `numbers`"),
    ({"op": "create_company", "fields": {"website": "x"}}, "fields.name"),
    ({"op": "add_tags", "item_id": "cll-1"}, "`tags` is required"),
    ({"op": "create_tag", "name": "Hot", "item_id": "cll-1"}, "does not take `item_id`"),
    ({"op": "create_note", "content": "x"}, "needs `person_id` or `contact_number`"),
    ({"op": "create_note", "content": "x", "person_id": "per-1", "contact_number": "+33"},
     "not both"),
])
def test_write_refusals(client, kwargs, needle):
    with pytest.raises(McpError, match=needle):
        _tool("allo_act")(**kwargs)
    assert client.built == []


# --- upstream refusals ------------------------------------------------------------

@pytest.mark.parametrize("status,needle", [
    (401, "refused the API key"), (403, "lacks the scope"), (404, "not found"),
    (429, "retry in 3 seconds"), (503, "temporarily unavailable"),
])
def test_upstream_refusals_translated(client, status, needle):
    client.get_person.side_effect = _upstream(status, {"error": {
        "code": "X_CODE", "message": "msg", "retry_after_seconds": 3}})
    with pytest.raises(McpError, match=needle):
        _tool("allo")(op="person", person_id="per-1")


def test_validation_error_keeps_allo_s_suggestion(client):
    client.create_tag.side_effect = _upstream(400, {"error": {
        "code": "VALIDATION_ERROR", "message": "name is blank",
        "suggestion": "Send a non-empty name."}})
    with pytest.raises(McpError, match="VALIDATION_ERROR name is blank Send a non-empty name."):
        _tool("allo_act")(op="create_tag", name=" ")


# --- probe ------------------------------------------------------------------------

def _probe_with(monkeypatch, error=None):
    inst = MagicMock()
    if error is not None:
        inst.probe.side_effect = error
    monkeypatch.setattr("oto.tools.allo.AlloClient", lambda **kw: inst)
    from oto_mcp.tools import allo as A
    return A._verify


def test_probe_ok(monkeypatch):
    _probe_with(monkeypatch)({"key": "ak_test"})


@pytest.mark.parametrize("status", [401, 403])
def test_probe_refused(monkeypatch, status):
    with pytest.raises(cv.NonAutorise):
        _probe_with(monkeypatch, _upstream(status))({"key": "ak_test"})


def test_probe_other_error_unclassified(monkeypatch):
    with pytest.raises(RuntimeError):
        _probe_with(monkeypatch, _upstream(500))({"key": "ak_test"})

"""`data_review_app` — la file de revue rendue, la seule app qui écrit.

On monte la VRAIE app (prefab_ui + FastMCPApp) sur une instance FastMCP nue, avec un
store en mémoire : on prouve que l'outil du bouton n'est pas listé au modèle, que la
carte l'adresse sous son nom haché avec le contexte figé, que le clic n'écrit que le
statut d'une ligne encore à `pending`, et que le contexte est reposé par les gardes
des axes. Pas de DB, pas de host. Les refus s'éprouvent sur leur CODE et sur
l'absence d'écriture, jamais sur leur phrase (`docs/conventions.md`).
"""
import asyncio

import pytest

pytest.importorskip("prefab_ui")

from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.providers.addressing import hashed_backend_name  # noqa: E402
from mcp.types import INVALID_PARAMS  # noqa: E402

from oto_mcp.datastore.core import RowNotFound  # noqa: E402

SCHEMA = {
    "key": "contactLinkedin",
    "fields": [
        {"key": "contact", "type": "text", "label": "Prospect", "display": "title"},
        {"key": "companyName", "type": "text", "label": "Entreprise"},
        {"key": "statut", "role": "status", "type": "enum", "label": "Statut",
         "options": ["to_review", "launched", "skipped"]},
        {"key": "offre", "type": "text", "label": "Offre pitchée"},
        {"key": "aRelire", "role": "note", "type": "text"},
    ],
}


def _rows():
    return [
        {"_id": "r1", "contact": "Jane Doe", "companyName": "Acme",
         "statut": "to_review", "offre": "Paid Media", "aRelire": "persona en repli"},
        {"_id": "r2", "contact": "John Roe", "companyName": "Globex",
         "statut": "to_review", "offre": "Rev Ops"},
        {"_id": "r3", "contact": "Already launched", "statut": "launched"},
    ]


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.writes: list = []

    def resolve_ns_id(self, datastore):
        return 657

    def get_schema(self, datastore):
        return SCHEMA

    @staticmethod
    def _match(row, filter):
        return all(str(row.get(k)) == str(v) for k, v in (filter or {}).items())

    def count_rows(self, datastore, *, filter=None, **_):
        return sum(1 for r in self.rows if self._match(r, filter))

    def list_rows(self, datastore, filter=None, limit=100, **_):
        return [r for r in self.rows if self._match(r, filter)][:limit]

    def get_row(self, datastore, row_id, **_):
        for r in self.rows:
            if r["_id"] == row_id:
                return dict(r)
        raise RowNotFound(row_id)

    def update_row(self, datastore, row_id, patch, **_):
        self.writes.append((datastore, row_id, patch))
        for r in self.rows:
            if r["_id"] == row_id:
                r.update(patch)
                return dict(r)
        raise RowNotFound(row_id)


def _nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nodes(v)


def _texts(result) -> str:
    return " | ".join(n["content"] for n in _nodes(result.structured_content)
                      if isinstance(n.get("content"), str))


def _tool_calls(result) -> list[dict]:
    return [n for n in _nodes(result.structured_content) if n.get("action") == "toolCall"]


def _chat_messages(result) -> list[dict]:
    return [n for n in _nodes(result.structured_content) if n.get("action") == "sendMessage"]


def _code(exc: BaseException):
    """Le code JSON-RPC d'un refus, où que fastmcp l'ait enveloppé."""
    seen = exc
    while seen is not None:
        code = getattr(getattr(seen, "error", None), "code", None)
        if code is not None:
            return code
        seen = seen.__cause__ or seen.__context__
    return None


@pytest.fixture
def served(monkeypatch):
    from oto_mcp.tools import datastore_review_app as mod

    store = _FakeStore(_rows())
    monkeypatch.setattr(mod, "make_store", lambda sub: store)
    monkeypatch.setattr(mod.access, "current_user_sub_or_raise", lambda: "sub1")
    monkeypatch.setattr(mod.access, "resolve_datastore_ref", lambda d: d)
    monkeypatch.setattr(mod.access, "current_project", lambda: 42)
    monkeypatch.setattr(mod.access, "current_org", lambda sub: 7)
    pins: list = []

    class _Axis:
        def __init__(self, param):
            self.param = param

        async def pin_for(self, value, tool_name):
            pins.append((self.param, value, tool_name))
            return []

    monkeypatch.setattr(mod.call_axes, "PROJECT", _Axis("_project"))
    monkeypatch.setattr(mod.call_axes, "ORG", _Axis("_org"))
    mcp = FastMCP("t")
    mod.register(mcp)
    return mcp, store, pins, mod


def _call(mcp, name, args):
    return asyncio.run(mcp.call_tool(name, args))


OPEN = {"datastore": "657", "pending": "to_review", "approve": "launched",
        "reject": "skipped", "approve_label": "Launch"}


def _decide(mod):
    return hashed_backend_name(mod.APP_NAME, "data_review_decide")


def test_only_the_entry_point_is_listed_to_the_model(served):
    mcp, *_ = served
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "data_review_app" in names
    assert not any("data_review_decide" in n for n in names)


def test_card_shows_next_pending_row_with_two_hashed_buttons(served):
    mcp, store, _, mod = served
    res = _call(mcp, "data_review_app", OPEN)
    texts = _texts(res)
    assert "Jane Doe" in texts and "Acme" in texts and "persona en repli" in texts
    assert "Already launched" not in texts
    calls = _tool_calls(res)
    assert len(calls) == 2
    assert {c["tool"] for c in calls} == {_decide(mod)}
    assert {c["arguments"]["value"] for c in calls} == {"launched", "skipped"}
    # contexte figé au rendu : le clic arrivera sans axes
    assert all(c["arguments"]["project"] == 42 and c["arguments"]["org"] == 7
               and c["arguments"]["id"] == "r1" and c["arguments"]["column"] == "statut"
               for c in calls)
    assert store.writes == []


def test_click_writes_status_only_and_moves_to_next_row(served):
    mcp, store, pins, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    res = _call(mcp, _decide(mod), {**args, "value": "launched"})
    assert store.writes == [("657", "r1", {"statut": "launched"})]
    assert {c["arguments"]["id"] for c in _tool_calls(res)} == {"r2"}
    assert pins == [("_project", 42, "data_review_decide")]


def test_click_result_swaps_the_card_through_its_view(served):
    """Le renderer range `structuredContent` dans `$result`, et un Slot ne peint qu'un
    COMPOSANT (clé `type` au premier niveau). Le gestionnaire rend une enveloppe
    PrefabApp, qui n'en a pas : si le bouton pose `$result` entier, la carte ne passe
    jamais à la suivante — l'écriture a lieu, l'écran ment. Vécu en cliquant dans un
    vrai host ; les autres tests, qui ne lisent que le JSON serveur, ne le voyaient pas."""
    mcp, store, _, mod = served
    res = _call(mcp, "data_review_app", OPEN)
    swaps = [n for n in _nodes(res.structured_content)
             if n.get("action") == "setState" and n.get("key") == "carte"]
    assert len(swaps) == 2 and all(s["value"] == "{{ $result.view }}" for s in swaps)
    args = _tool_calls(res)[0]["arguments"]
    after = _call(mcp, _decide(mod), {**args, "value": "launched"}).structured_content
    assert "type" not in after and after["view"].get("type")
    # la carte suivante porte le même câblage, sinon le 2e clic retomberait dans le défaut
    assert all(n["value"] == "{{ $result.view }}" for n in _nodes(after)
               if n.get("action") == "setState" and n.get("key") == "carte")


def test_org_is_repinned_when_the_card_had_no_project(served):
    mcp, store, pins, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    _call(mcp, _decide(mod), {**args, "project": None, "value": "skipped"})
    assert pins == [("_org", 7, "data_review_decide")]
    assert store.writes == [("657", "r1", {"statut": "skipped"})]


def test_row_changed_meanwhile_is_skipped_not_overwritten(served):
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    store.rows[0]["statut"] = "skipped"  # quelqu'un d'autre a tranché
    res = _call(mcp, _decide(mod), {**args, "value": "launched"})
    assert store.writes == []
    assert {c["arguments"]["id"] for c in _tool_calls(res)} == {"r2"}


def test_click_with_a_value_outside_the_card_is_refused(served):
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    with pytest.raises(Exception) as refus:
        _call(mcp, _decide(mod), {**args, "value": "interesse"})
    assert _code(refus.value) == INVALID_PARAMS
    assert store.writes == []


def test_values_outside_declared_options_open_no_buttons(served):
    mcp, store, *_ = served
    res = _call(mcp, "data_review_app", {**OPEN, "approve": "approved"})
    assert _tool_calls(res) == []
    assert store.writes == []


def test_app_only_handler_bypasses_visibility_transforms(served):
    """Ce que le module et `docs/mcp-apps.md` affirment, JOUÉ : une règle de visibilité
    (la même classe de transform que la denylist de session) bloque un tool normal —
    et laisse passer le gestionnaire app-only sous son nom haché. C'est pourquoi il ne
    peut compter que sur ses propres gardes."""
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    mcp.disable(names={"data_review_app", "data_review_decide"})
    with pytest.raises(Exception):
        _call(mcp, "data_review_app", OPEN)  # le transform mord sur le chemin normal
    _call(mcp, _decide(mod), {**args, "value": "launched"})
    assert store.writes == [("657", "r1", {"statut": "launched"})]


def test_a_value_the_renderer_would_interpolate_opens_no_buttons(served):
    """`CallTool.arguments` interpole `{{ clé }}` côté client : figée dans un bouton,
    la valeur n'arriverait pas telle qu'écrite."""
    mcp, store, *_ = served
    res = _call(mcp, "data_review_app", {**OPEN, "filter": {"offre": "{{ x }}"}})
    assert _tool_calls(res) == []
    assert store.writes == []


def test_handler_is_unreachable_by_its_plain_name(served):
    mcp, store, *_ = served
    with pytest.raises(Exception):
        _call(mcp, "data_review_decide", {"datastore": "657", "id": "r1", "value": "launched",
                                          "column": "statut", "pending": "to_review",
                                          "approve": "launched", "reject": "skipped"})
    assert store.writes == []


def test_empty_queue_offers_no_button(served):
    mcp, store, *_ = served
    for r in store.rows:
        r["statut"] = "launched"
    res = _call(mcp, "data_review_app", OPEN)
    assert _tool_calls(res) == []
    assert _chat_messages(res) == []  # rien tranché dans cette carte : rien à résumer


def test_tally_rides_the_buttons_and_moves_on_a_write(served):
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    assert (args["approve_count"], args["reject_count"]) == (0, 0)
    res = _call(mcp, _decide(mod), {**args, "value": "launched"})
    nxt = _tool_calls(res)[0]["arguments"]
    assert (nxt["approve_count"], nxt["reject_count"]) == (1, 0)


def test_tally_does_not_move_when_nothing_is_written(served):
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    store.rows[0]["statut"] = "skipped"  # tranché ailleurs entre-temps
    res = _call(mcp, _decide(mod), {**args, "value": "launched"})
    nxt = _tool_calls(res)[0]["arguments"]
    assert (nxt["approve_count"], nxt["reject_count"]) == (0, 0)
    del store.rows[1]  # la ligne suivante disparaît avant le clic
    res = _call(mcp, _decide(mod), {**nxt, "value": "launched"})
    assert store.writes == []
    assert _tool_calls(res) == [] and _chat_messages(res) == []


def test_a_forged_tally_is_clamped_and_never_guards(served):
    """Le bilan revient du client : rejouable, modifiable. Borné et affiché, il ne
    décide jamais d'une écriture."""
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    res = _call(mcp, _decide(mod), {**args, "approve_count": -7,
                                    "reject_count": 10**9, "value": "launched"})
    nxt = _tool_calls(res)[0]["arguments"]
    assert nxt["approve_count"] == 1 and nxt["reject_count"] == 100_000
    assert store.writes == [("657", "r1", {"statut": "launched"})]


def test_done_queue_offers_one_chat_message_and_no_tool_call(served):
    mcp, store, _, mod = served
    args = _tool_calls(_call(mcp, "data_review_app", OPEN))[0]["arguments"]
    args = _tool_calls(_call(mcp, _decide(mod), {**args, "value": "launched"}))[0]["arguments"]
    res = _call(mcp, _decide(mod), {**args, "value": "skipped"})
    assert _tool_calls(res) == []
    messages = _chat_messages(res)
    assert len(messages) == 1
    assert "1 launched" in messages[0]["content"] and "1 skipped" in messages[0]["content"]

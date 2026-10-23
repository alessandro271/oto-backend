"""Slack — un message peut partir SANS aperçu de liens (`unfurl=False`).

Des agents postent des liens dans un canal et ne veulent pas que Slack déplie
une carte sous chacun. Le client de la lib porte `unfurl_links` et `unfurl_media`,
envoyés à Slack seulement quand ils sont donnés. L'outil sert un seul `unfurl` ;
`false` passe les deux drapeaux à False, en mots-clés écrits, pour que la sonde de
version (`test_client_kwargs_exist_on_pinned_core`) les voie face à la lib épinglée.
Par défaut, l'appel au client est exactement celui d'avant.
"""
import ast
import asyncio
import pathlib
from unittest.mock import patch

from oto_mcp.tools import slack as slack_tools


def _monte(monkeypatch):
    class _RC:
        fields = {"bot_token": "xoxb-1", "user_token": None}
        key = None
        is_platform = False

    monkeypatch.setattr(slack_tools.access, "resolve_credential", lambda *a, **k: _RC())
    from fastmcp import FastMCP
    patcher = patch("oto.tools.slack.client.SlackClient")
    cls = patcher.start()
    m = FastMCP("t")
    slack_tools.register(m)
    client = cls.return_value
    client.post_message.return_value = {"ok": True, "ts": "1.2"}
    client.channel_info.return_value = {"ok": True, "channel": {
        "id": "C1", "name": "seo", "is_im": False, "is_ext_shared": False}}
    return m, client, patcher


def _poste(m, **kw):
    return asyncio.run(m.get_tool("slack_post_message")).fn(channel="C1", text="https://example.com", **kw)


def test_unfurl_false_est_transmis_au_client(monkeypatch):
    m, client, patcher = _monte(monkeypatch)
    try:
        _poste(m, unfurl=False)
    finally:
        patcher.stop()
    kw = client.post_message.call_args.kwargs
    assert kw["unfurl_links"] is False and kw["unfurl_media"] is False


def test_par_defaut_l_appel_au_client_ne_change_pas(monkeypatch):
    m, client, patcher = _monte(monkeypatch)
    try:
        _poste(m)
    finally:
        patcher.stop()
    assert client.post_message.call_args.kwargs == {"text": "https://example.com",
                                                    "thread_ts": None}


def test_unfurl_false_arrive_jusqu_a_chat_post_message(monkeypatch):
    """Bout à bout avec le VRAI client de la lib, seul `_request` (le HTTP) doublé :
    `unfurl=False` pose `unfurl_links`/`unfurl_media` à false sur chaque partie,
    le défaut n'en pose aucun."""
    from oto.tools.slack.client import SlackClient

    class _RC:
        fields = {"bot_token": "xoxb-1", "user_token": None}
        key = None
        is_platform = False

    monkeypatch.setattr(slack_tools.access, "resolve_credential", lambda *a, **k: _RC())
    envois = []

    def _faux(self, method, endpoint, as_user=None, **kw):
        if endpoint == "chat.postMessage":
            envois.append(kw["json"])
            return {"ok": True, "ts": f"1.{len(envois)}"}
        return {"ok": True, "channel": {"id": "C1", "name": "seo"}}

    monkeypatch.setattr(SlackClient, "_request", _faux)
    from fastmcp import FastMCP
    m = FastMCP("t")
    slack_tools.register(m)

    _poste(m, unfurl=False)
    assert envois[-1]["unfurl_links"] is False and envois[-1]["unfurl_media"] is False

    envois.clear()
    asyncio.run(m.get_tool("slack_post_message")).fn(
        channel="C1", text="https://example.com " + "x" * 9000, unfurl=False)
    assert len(envois) > 1
    assert all(e["unfurl_links"] is False and e["unfurl_media"] is False for e in envois)

    envois.clear()
    _poste(m)
    assert "unfurl_links" not in envois[-1] and "unfurl_media" not in envois[-1]


def test_les_drapeaux_sont_des_mots_cles_ecrits_que_la_sonde_de_version_voit():
    """Un `**({} if unfurl else {...})` déballé échappait à la sonde des mots-clés
    (`test_client_kwargs_exist_on_pinned_core`) : mergé sur une lib sans ces
    drapeaux, il passait la CI et levait TypeError au premier `unfurl=false` en prod."""
    arbre = ast.parse(pathlib.Path(slack_tools.__file__).read_text())
    appels = [n for n in ast.walk(arbre) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "post_message"]
    assert any({"unfurl_links", "unfurl_media"} <= {k.arg for k in a.keywords}
               for a in appels)
    assert all(k.arg is not None for a in appels for k in a.keywords)


def test_le_parametre_est_annonce_dans_la_description():
    from fastmcp import FastMCP
    m = FastMCP("t")
    slack_tools.register(m)
    tool = asyncio.run(m.get_tool("slack_post_message"))
    prop = tool.parameters["properties"]["unfurl"]
    assert "preview" in (prop.get("description") or "")

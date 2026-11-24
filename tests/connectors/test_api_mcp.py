"""A Custom API whose protocol is `mcp` (`connectors/api_mcp.py`): one short Streamable
HTTP session per verb, the card's auth on every request, the admin's allowlist applied,
no redirect followed. A fake server stands in for the remote MCP."""
from __future__ import annotations

import json

import pytest
import requests

from oto_mcp.connectors import api_mcp

CARD = {"base_url": "https://mcp.example.test/mcp", "protocol": "mcp",
        "auth_mode": "bearer", "token": "secret-token"}
TOOLS = [{"name": "search_accounts", "description": "Search accounts",
          "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
         {"name": "update_deal", "description": "Update a deal",
          "inputSchema": {"type": "object"}}]


class _Rep:
    def __init__(self, status=200, body=None, sse=False, headers=None):
        self.status_code = status
        self.headers = {**(headers or {})}
        if body is None:
            raw = b""
        elif sse:
            raw = f"event: message\ndata: {json.dumps(body)}\n\n".encode()
            self.headers["Content-Type"] = "text/event-stream"
        else:
            raw = json.dumps(body).encode()
            self.headers["Content-Type"] = "application/json"
        self._raw = raw

    def iter_content(self, n):
        yield self._raw


class _Serveur:
    """Answers initialize / tools/list / tools/call like a Streamable HTTP server."""

    def __init__(self, sse=False, status=None):
        self.recus: list[tuple[dict, dict]] = []
        self.sse, self.status, self.fermee = sse, status, False
        self.session_id: str | None = "sess-1"
        self.version = api_mcp.PROTOCOL_VERSION

    def post(self, url, data=None, headers=None, timeout=None, stream=None,
             allow_redirects=None, **_):
        assert url == CARD["base_url"] and allow_redirects is False
        msg = json.loads(data)
        self.recus.append((msg, dict(headers or {})))
        if self.status:
            return _Rep(self.status)
        m = msg.get("method")
        if "id" not in msg:
            return _Rep(202)
        if m == "initialize":
            return _Rep(body={"jsonrpc": "2.0", "id": msg["id"],
                              "result": {"protocolVersion": self.version}},
                        headers={"Mcp-Session-Id": self.session_id}
                        if self.session_id else {})
        if m == "tools/list":
            return _Rep(body={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}},
                        sse=self.sse)
        if m == "tools/call":
            p = msg["params"]
            return _Rep(body={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": f"ok {p['name']} {p['arguments']}"}],
                "structuredContent": {"accounts": [{"name": "Acme", "note": "é"}]},
                "isError": False}}, sse=self.sse)
        return _Rep(body={"jsonrpc": "2.0", "id": msg["id"],
                          "error": {"code": -32601, "message": "unknown"}})

    def delete(self, url, headers=None, **_):
        self.fermee = headers.get("Mcp-Session-Id") == "sess-1"
        return _Rep(200)


@pytest.fixture
def serveur(monkeypatch):
    srv = _Serveur()
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda *a, **k: None)

    vraie = requests.Session

    def session():
        s = vraie()
        s.post = lambda *a, **k: srv.post(*a, **k)
        s.delete = lambda *a, **k: srv.delete(*a, **k)
        srv.sessions.append(s)
        return s
    srv.sessions = []
    monkeypatch.setattr(api_mcp.requests, "Session", session)
    return srv


def test_la_liste_ouvre_une_session_authentifiee_et_la_ferme(serveur):
    out = api_mcp.list_tools(CARD, None, {})
    assert [t["name"] for t in out["tools"]] == ["search_accounts", "update_deal"]
    assert out["tools"][0]["input_schema"]["properties"]["q"]["type"] == "string"
    methodes = [m.get("method") for m, _ in serveur.recus]
    assert methodes == ["initialize", "notifications/initialized", "tools/list"]
    # The card's key on every request, the session id once initialized.
    assert all(h.get("Authorization") is None for _, h in serveur.recus)  # set on session
    assert serveur.recus[2][1]["Mcp-Session-Id"] == "sess-1"
    assert serveur.fermee


def test_le_jeton_part_sur_la_session(serveur):
    api_mcp.list_tools(CARD, None, {})
    assert serveur.sessions[0].headers["Authorization"] == "Bearer secret-token"


def test_une_reponse_sse_est_lue(serveur):
    serveur.sse = True
    out = api_mcp.call_tool(CARD, None, {}, "search_accounts", {"q": "acme"})
    assert out["tool"] == "search_accounts" and out["is_error"] is False
    assert "acme" in out["content"][0]["text"]


def test_la_liste_blanche_filtre_et_refuse(serveur):
    d = {"mcp_tools": ["search_accounts"]}
    out = api_mcp.list_tools(CARD, None, d)
    assert [t["name"] for t in out["tools"]] == ["search_accounts"] and out["hidden"] == 1
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp.call_tool(CARD, None, d, "update_deal", {})
    assert e.value.code == "mcp_tool_not_allowed"
    # Refused BEFORE anything is sent.
    assert not any(m.get("method") == "tools/call" for m, _ in serveur.recus)


def test_un_refus_de_cle_est_nomme(serveur):
    serveur.status = 401
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp.list_tools(CARD, None, {})
    assert e.value.code == "mcp_unauthorized"


def test_une_redirection_n_est_jamais_suivie(serveur):
    serveur.status = 302
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp.list_tools(CARD, None, {})
    assert e.value.code == "mcp_redirect"


def test_le_mode_session_est_refuse(serveur):
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp.list_tools({**CARD, "auth_mode": "session"}, None, {})
    assert e.value.code == "mcp_mode_unsupported"


def test_oauth2_user_rejoue_une_fois_apres_un_jeton_refuse(monkeypatch, serveur):
    from oto_mcp.auth import api_oauth
    jetons = iter(["vieux", "neuf"])
    oublies = []
    monkeypatch.setattr(api_oauth, "access_token_for", lambda row, f: next(jetons))
    monkeypatch.setattr(api_oauth, "oublier", lambda row: oublies.append(row))
    orig = serveur.post
    essais = {"n": 0}

    def post(url, **k):
        if essais["n"] == 0:
            essais["n"] += 1
            return _Rep(401)
        return orig(url, **k)
    serveur.post = post
    row = ("member", "1:s", "crm")
    out = api_mcp.list_tools({**CARD, "auth_mode": "oauth2_user"}, row, {})
    assert out["count"] == 2 and oublies == [row]


def test_un_outil_est_requis():
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp.call_tool(CARD, None, {}, None, {})
    assert e.value.code == "mcp_tool_required"


def test_la_definition_accepte_la_liste_blanche():
    from oto_mcp import custom_ops, custom_ops_definition as d
    assert d.validate_definition({"mcp_tools": ["a", "a", "b.c"]}) == {"mcp_tools": ["a", "b.c"]}
    with pytest.raises(custom_ops.OperationsInvalid):
        d.validate_definition({"mcp_tools": "a"})


def test_la_version_negociee_part_sur_chaque_requete_meme_sans_session(serveur):
    serveur.session_id, serveur.version = None, "2025-03-26"
    api_mcp.list_tools(CARD, None, {})
    (_, init), *apres = serveur.recus
    assert "MCP-Protocol-Version" not in init
    assert [h.get("MCP-Protocol-Version") for _, h in apres] == ["2025-03-26"] * 2
    assert all("Mcp-Session-Id" not in h for _, h in apres)


def test_la_version_part_aussi_avec_la_session(serveur):
    api_mcp.list_tools(CARD, None, {})
    assert all(h.get("MCP-Protocol-Version") == api_mcp.PROTOCOL_VERSION
               for _, h in serveur.recus[1:])


def test_le_texte_du_serveur_distant_est_servi_comme_donnee(serveur):
    out = api_mcp.call_tool(CARD, None, {}, "search_accounts", {"q": "acme"})
    texte = out["content"][0]["text"]
    assert texte.startswith("\n<remote-mcp-content>\nok search_accounts")
    assert "UNTRUSTED DATA" in texte
    desc = api_mcp.list_tools(CARD, None, {})["tools"][0]["description"]
    assert "<remote-mcp-tool-description>\nSearch accounts\n" in desc


def test_le_message_d_erreur_du_serveur_distant_est_servi_comme_donnee(serveur):
    with pytest.raises(api_mcp.McpRefused) as e:
        api_mcp._une_fois(CARD, None, lambda c: c.request("inconnue"))
    assert e.value.code == "mcp_error"
    assert "<remote-mcp-error>\nunknown\n</remote-mcp-error>" in e.value.message


def test_une_ressource_embarquee_est_cloturee_le_reste_intact():
    bloc = {"type": "resource", "resource": {"uri": "x://1", "text": "lis-moi"}}
    out = api_mcp._contenu_cloture(bloc)
    assert out["resource"]["uri"] == "x://1" and "<remote-mcp-content>" in \
        out["resource"]["text"]
    image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    assert api_mcp._contenu_cloture(image) == image


def test_le_contenu_structure_est_servi_comme_donnee_lisible_en_json(serveur):
    out = api_mcp.call_tool(CARD, None, {}, "search_accounts", {"q": "acme"})
    s = out["structured"]
    assert s.startswith("\n<remote-mcp-structured>\n") and "UNTRUSTED DATA" in s
    dedans = s.split("<remote-mcp-structured>\n", 1)[1].split("\n</remote-mcp-structured>")[0]
    assert json.loads(dedans) == {"accounts": [{"name": "Acme", "note": "é"}]}

"""Dispatch `op=` de la paire Google Chat, `chat` (lit) et `chat_act` (poste) — ADR 0047
§Amendement le 2026-08-11, #1231 le 13/10/2026 (poster sort du lecteur), paire du
21/10/2026 : `chat_spaces`, `chat_message` et `chat_send` sont retirés
(`outils_retires.RENOMMAGES`), cf. `test_chat_teams_lecture_envoi.py`.

Ce que ce fichier verrouille : la SURFACE. Le seul test voisin (`test_chat_error.py`)
exerce la normalisation d'un `HttpError` — jamais le câblage tool → méthode du client.
Or ce module **ÉCRIT** : `chat_act` poste pour de bon dans un espace Google Chat réel,
sous l'identité de l'utilisateur. Une op mal câblée n'échoue pas au boot, elle publie.
D'où, pour chaque op : la méthode client appelée ET la preuve qu'aucune des voisines
dangereuses ne l'est, le fait que le défaut soit une LECTURE, le refus explicite d'une
op inconnue (jamais un fallback muet) et le refus nommé d'un argument obligatoire.

Les tools sont `async` (le client Google est sync, poussé hors boucle par
`asyncio.to_thread`) → on les déroule par `asyncio.run`, comme `_tool` les récupère.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError
def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import chat as C

    m = FastMCP("t")
    C.register(m)
    fn = asyncio.run(m.get_tool(name)).fn
    return lambda **kw: asyncio.run(fn(**kw))


@pytest.fixture
def client(monkeypatch):
    """Faux ChatClient — le credential OAuth Google n'est pas le sujet ici.

    `_client_for_user` résout le `sub` courant puis les credentials du compte : on
    patche le tout au seam du module, en gardant l'`account` reçu pour vérifier
    qu'il est bien transmis.
    """
    from oto_mcp.tools import chat as C

    inst = MagicMock()
    inst.accounts = []
    monkeypatch.setattr(C, "_client_for_user",
                        lambda account=None: (inst.accounts.append(account), inst)[1])
    return inst


# --- espaces : la découverte, `chat(op="spaces")`, le défaut ---------------------

def test_spaces_lists_spaces_and_counts_them(client):
    client.list_spaces.return_value = [{"name": "spaces/A"}, {"name": "spaces/B"}]
    assert _tool("chat")() == {
        "spaces": [{"name": "spaces/A"}, {"name": "spaces/B"}], "count": 2}
    client.list_spaces.assert_called_once()


def test_spaces_builds_the_chat_api_filter(client):
    """Le tool prend un `space_type` nu et fabrique le filtre attendu par l'API Chat
    (`spaceType = "…"`) — pas de filtre du tout quand il est omis."""
    client.list_spaces.return_value = []
    _tool("chat")(op="spaces", space_type="DIRECT_MESSAGE", max_results=7)
    assert client.list_spaces.call_args.args == ('spaceType = "DIRECT_MESSAGE"', 7)
    _tool("chat")()
    assert client.list_spaces.call_args.args == (None, 100)


def test_spaces_never_writes(client):
    """L'inventaire est une lecture : aucune des deux méthodes d'envoi ne bouge."""
    client.list_spaces.return_value = []
    _tool("chat")()
    client.send.assert_not_called()
    client.send_dm.assert_not_called()


# --- messages : lecture --------------------------------------------------------

def test_spaces_refuses_a_space(client):
    """`space` ne sert qu'à op='list' : sur la découverte, c'est une erreur d'intention,
    refusée avant toute résolution de compte."""
    with pytest.raises(McpError, match="op='list'"):
        _tool("chat")(space="spaces/A")
    assert client.mock_calls == [] and client.accounts == []


def test_message_list_reads_and_never_writes(client):
    """Lire les messages d'un espace ne poste jamais dans un espace réel."""
    client.list_messages.return_value = [{"text": "hello"}]
    out = _tool("chat")(op="list", space="spaces/A")
    assert out == {"messages": [{"text": "hello"}], "count": 1}
    client.list_messages.assert_called_once()
    client.send.assert_not_called()
    client.send_dm.assert_not_called()


def test_message_list_forwards_the_space_and_the_cap(client):
    client.list_messages.return_value = []
    _tool("chat")(op="list", space="spaces/A", max_results=5)
    assert client.list_messages.call_args.args == ("spaces/A", 5)
    _tool("chat")(op="list", space="spaces/A")
    assert client.list_messages.call_args.args == ("spaces/A", 20)


def test_message_list_requires_the_space(client):
    """Pas de repli sur « le premier espace venu » : l'espace est nommé."""
    with pytest.raises(McpError, match="space") as e:
        _tool("chat")(op="list")
    assert "op='list'" in str(e.value) and "op='spaces'" in str(e.value)
    with pytest.raises(McpError, match="space_type"):
        _tool("chat")(op="list", space="spaces/A", space_type="SPACE")
    client.list_messages.assert_not_called()
    assert client.accounts == []


# --- chat_act : ÉCRITURE (poste dans un espace Google Chat réel) --------------

def test_message_send_to_a_space_posts_only_there(client):
    """`space` → `send(space, text)` et RIEN d'autre : ni DM (qui partirait à un
    tiers), ni lecture."""
    _tool("chat_act")(space="spaces/A", text="bonjour")
    client.send.assert_called_once()
    assert client.send.call_args.args == ("spaces/A", "bonjour")
    client.send_dm.assert_not_called()
    client.list_messages.assert_not_called()
    client.list_spaces.assert_not_called()


def test_message_send_to_a_user_resolves_the_dm_and_nothing_else(client):
    """`user` → `send_dm(user, text)` (qui résout l'espace DM côté client) : `send`
    ne doit PAS être appelée avec un email en guise d'espace."""
    _tool("chat_act")(user="marie@exemple.fr", text="bonjour")
    client.send_dm.assert_called_once()
    assert client.send_dm.call_args.args == ("marie@exemple.fr", "bonjour")
    client.send.assert_not_called()
    client.list_messages.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {},                                                   # aucune destination
    {"space": "spaces/A", "user": "marie@exemple.fr"},     # les deux
])
def test_message_send_refuses_an_ambiguous_destination(client, kwargs):
    """Ni deviné, ni « les deux » : un envoi dont la destination est ambiguë est
    refusé AVANT d'atteindre Google — sinon le message part au mauvais endroit — et
    avant toute résolution de compte."""
    with pytest.raises(McpError, match="either"):
        _tool("chat_act")(text="bonjour", **kwargs)
    client.send.assert_not_called()
    client.send_dm.assert_not_called()
    assert client.accounts == []


@pytest.mark.parametrize("dest", [{"space": "spaces/A"}, {"user": "marie@exemple.fr"}])
def test_message_send_requires_the_text(client, dest):
    """`text` est exigé par la signature de `chat_act`, et un texte vide passe la
    signature : c'est ce check runtime qui interdit de poster un message vide."""
    for vide in (None, ""):
        with pytest.raises(McpError, match="text") as e:
            _tool("chat_act")(text=vide, **dest)
        assert "chat_act" in str(e.value)
    client.send.assert_not_called()
    client.send_dm.assert_not_called()
    assert client.accounts == []


# --- refus ---------------------------------------------------------------------

def test_unknown_op_is_refused_with_the_allowed_list(client):
    """Une op inconnue doit lever en nommant les ops valides — jamais retomber
    silencieusement sur le défaut (l'agent croirait sa demande honorée), et surtout
    jamais sur l'envoi."""
    with pytest.raises(McpError, match="op must be") as e:
        _tool("chat")(op="metadata", space="spaces/A")
    assert "'list'" in str(e.value) and "`chat_act`" in str(e.value)
    assert client.mock_calls == [] and client.accounts == []


def test_no_op_reaches_a_write_by_accident(client):
    """Filet global : aucune valeur d'`op` du lecteur n'atteint `send`/`send_dm`, y
    compris `send` et les quasi-synonymes qu'un agent pourrait inventer."""
    for op in ("send", "metadata", "get", "read", "post", "create", "delete", "SEND", ""):
        with pytest.raises(McpError):
            _tool("chat")(op=op, space="spaces/A")
    client.send.assert_not_called()
    client.send_dm.assert_not_called()


# --- compte ciblé ---------------------------------------------------------------

def test_account_is_forwarded_by_every_tool(client):
    """Multi-compte : `account` doit atteindre la résolution de credentials, sinon
    on lirait/posterait depuis le mauvais compte Google."""
    client.list_spaces.return_value = []
    client.list_messages.return_value = []
    client.send.return_value = {"name": "spaces/A/messages/1"}
    _tool("chat")(account="alexis@otomata.tech")
    _tool("chat")(op="list", space="spaces/A", account="alexis@otomata.tech")
    _tool("chat_act")(space="spaces/A", text="ok", account="alexis@otomata.tech")
    assert client.accounts == ["alexis@otomata.tech"] * 3


# --- inventaire de surface ------------------------------------------------------

def test_the_product_exposes_exactly_its_pair(client):
    """`chat` lit (espaces et messages), `chat_act` poste : un client autorise des
    outils, pas des ops (21/10/2026)."""
    from fastmcp import FastMCP
    from oto_mcp.tools import chat as C

    m = FastMCP("t")
    C.register(m)
    names = {t.name for t in asyncio.run(m.list_tools())}
    assert names == {"chat", "chat_act"}


def test_nothing_is_required_by_the_schema_so_op_gates_everything(client):
    """Aucun argument n'est exigé par la SIGNATURE : c'est le dispatch `op` qui
    porte les obligations. Un tool d'écriture sans champ requis n'est sûr que parce
    que son défaut est une lecture — les tests ci-dessus le figent."""
    from fastmcp import FastMCP
    from oto_mcp.tools import chat as C

    m = FastMCP("t")
    C.register(m)
    msg = asyncio.run(m.get_tool("chat")).parameters
    assert msg.get("required", []) == []
    assert msg["properties"]["op"]["default"] == "spaces"


def test_chat_act_requires_its_text_in_the_schema(client):
    """L'écrivain exige `text` dès la signature ; la destination (l'un OU l'autre) reste
    au contrôle runtime."""
    from fastmcp import FastMCP
    from oto_mcp.tools import chat as C

    m = FastMCP("t")
    C.register(m)
    assert asyncio.run(m.get_tool("chat_act")).parameters["required"] == ["text"]

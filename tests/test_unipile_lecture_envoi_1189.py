"""Lecture et envoi dans des outils DIFFÉRENTS (#1189, décision du 09/10/2026 ; paires des
messageries le 21/10/2026).

Un client MCP (claude.ai, ChatGPT) autorise des OUTILS, pas des opérations : « toujours
autoriser » `whatsapp_chat` pour lire autorisait aussi l'envoi à un tiers — jusqu'au
21/10/2026, par son ancienne `op="send"` encore servie. Ce que ce fichier tient :

- les écrivains déclarent `destructiveHint` + `openWorldHint` ;
- l'écrivain vit sous le MÊME connecteur que son lecteur (même activation, même ACL,
  même compte opéré `_account=`) ;
- le lecteur sert ses lectures, et une op inconnue reste un refus de valeur ;
- un lecteur de MESSAGERIE (`whatsapp`, `linkedin_unipile`…) est `LECTURE` et ne porte
  aucune op d'envoi : il n'a pas de transition.

La transition qui reste (`linkedin_unipile_post` et `_network`, servies par leur
écrivain jusqu'au 10/12/2026) et sa garde datée vivent dans
`tests/test_unipile_transition_envoi_1189.py`.
"""
from __future__ import annotations

import asyncio
import typing
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from pydantic import TypeAdapter

from oto_mcp import call_axes, error_taxonomy
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import unipile as U
from oto_mcp.tools.lecture import en_lecture

CANAUX = ("whatsapp", "telegram", "instagram")

#: lecteur → (écrivain, ops de l'écrivain). La table de la décision, écrite une fois.
DECOUPAGE = {
    **{c: (f"{c}_act", ("send",)) for c in CANAUX},
    "linkedin_unipile": ("linkedin_unipile_act", ("send", "update", "react")),
    "linkedin_unipile_network": ("linkedin_unipile_invite", ("invite", "handle", "cancel")),
    "linkedin_unipile_post": ("linkedin_unipile_publish", ("create", "comment", "react")),
}

#: Les lecteurs qui servent ENCORE les ops de leur écrivain, jusqu'au retrait.
EN_TRANSITION = ("linkedin_unipile_network", "linkedin_unipile_post")

#: Ce qui reste au lecteur.
OPS_LUES = {
    **{c: ["list", "read"] for c in CANAUX},
    "linkedin_unipile": ["list", "read", "attendees", "contacts"],
    "linkedin_unipile_network": ["relations", "invitations"],
    "linkedin_unipile_post": ["feed", "get", "engagement"],
}


def _monte() -> FastMCP:
    m = FastMCP("t")
    U.register(m)
    for c in CANAUX:
        U.register_messaging_tools(m, c.upper())
    return m


@pytest.fixture
def m():
    return _monte()


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    appels = []
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: (appels.append(a), inst)[1])
    inst.appels = appels
    return inst


def _outil(m, nom):
    return asyncio.run(m.get_tool(nom))


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_declare_qu_il_agit_chez_un_tiers(m, lecteur):
    ecrivain = DECOUPAGE[lecteur][0]
    a = _outil(m, ecrivain).annotations
    assert a.readOnlyHint is False and a.destructiveHint is True
    assert a.openWorldHint is True


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_vit_sous_le_connecteur_de_son_lecteur(lecteur):
    """Même namespace ⟹ même activation, même ACL, même sélection — et le même axe
    `_account=` (comptes accordés, #55) : un envoi sous un AUTRE compte que la lecture
    serait pire que l'ancien outil."""
    ecrivain = DECOUPAGE[lecteur][0]
    assert namespace_of(ecrivain) == namespace_of(lecteur)
    assert call_axes.accepts_account_axis(ecrivain), ecrivain


def test_une_op_inconnue_reste_un_refus_de_valeur(m, client):
    """Une faute de frappe est la valeur refusée, avec les ops admises — jamais
    exécutée, jamais une clé résolue."""
    with pytest.raises(Exception) as e:
        asyncio.run(m.call_tool("whatsapp", {"op": "sned"}))
    msg = error_taxonomy.classify(e.value).message
    assert "'list'" in msg and "'read'" in msg, msg
    assert client.appels == []


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_les_lectures_passent_la_validation(m, lecteur):
    """Chaque lecture passe la validation de la signature telle qu'elle est montée
    (annotation comprise), et figure au schéma servi."""
    assert set(OPS_LUES[lecteur]) <= set(_outil(m, lecteur).parameters["properties"]
                                         ["op"]["enum"])
    ann = typing.get_type_hints(_outil(m, lecteur).fn, include_extras=True)["op"]
    for op in OPS_LUES[lecteur]:
        assert TypeAdapter(ann).validate_python(op) == op


@pytest.mark.parametrize("lecteur", sorted(set(DECOUPAGE) - set(EN_TRANSITION)))
def test_un_lecteur_de_messagerie_ne_sert_que_ses_lectures(m, lecteur):
    """Autoriser la lecture n'autorise plus l'envoi : le lecteur est `LECTURE` et son
    schéma ne porte aucune op de son écrivain — ni dépréciée, ni en transition."""
    t = _outil(m, lecteur)
    assert en_lecture(t), lecteur
    assert sorted(t.parameters["properties"]["op"]["enum"]) == sorted(OPS_LUES[lecteur])
    assert not {"text", "recipient_id", "message_id", "reaction", "action", "value"} & set(
        t.parameters["properties"]), lecteur


@pytest.mark.parametrize("lecteur", sorted(set(DECOUPAGE) - set(EN_TRANSITION)))
def test_un_lecteur_de_messagerie_n_envoie_jamais(m, client, lecteur):
    """`op="send"` sur le lecteur est une valeur refusée : rien n'est envoyé, aucune
    clé n'est résolue."""
    with pytest.raises(Exception):
        asyncio.run(m.call_tool(lecteur, {"op": "send", "text": "hello", "chat_id": "c1"}))
    assert client.appels == []
    client.send_message.assert_not_called()


@pytest.mark.parametrize("canal", CANAUX)
def test_l_ecrivain_d_un_canal_envoie_et_refuse_avant_toute_cle(m, client, canal):
    client.send_message.return_value = {"object": "send_message"}
    out = asyncio.run(m.call_tool(f"{canal}_act", {"op": "send", "text": "hello",
                                                   "chat_id": "c1"})).structured_content
    assert out == {"object": "send_message"}
    client.send_message.assert_called_once_with("hello", chat_id="c1", attendee_id=None)
    client.appels.clear()
    for args, manque in (({"text": "hello"}, "chat_id"), ({"chat_id": "c1"}, "text")):
        with pytest.raises(Exception) as e:
            asyncio.run(m.call_tool(f"{canal}_act", args))
        assert manque in error_taxonomy.classify(e.value).message
    assert client.appels == []

"""Lecture et envoi dans des outils DIFFÉRENTS (#1189, décision du 09/10/2026).

Un client MCP (claude.ai, ChatGPT) autorise des OUTILS, pas des opérations : « toujours
autoriser » `whatsapp_chat` pour lire autorisait aussi l'envoi à un tiers. Ce que ce
fichier tient, au-delà de la transition :

- les écrivains déclarent `destructiveHint` + `openWorldHint` ;
- l'écrivain vit sous le MÊME connecteur que son lecteur (même activation, même ACL,
  même compte opéré `_account=`) ;
- le lecteur sert ses lectures, et une op inconnue reste un refus de valeur.

La transition (les anciennes ops servies par l'écrivain jusqu'au 10/12/2026) et sa garde
datée vivent dans `tests/test_unipile_transition_envoi_1189.py`.
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

CANAUX = ("whatsapp", "telegram", "instagram")

#: lecteur → (écrivain, ops déplacées). La table de la décision, écrite une fois.
DECOUPAGE = {
    **{f"{c}_chat": (f"{c}_send", ("send",)) for c in CANAUX},
    "linkedin_unipile_chat": ("linkedin_unipile_send", ("send", "update", "react")),
    "linkedin_unipile_network": ("linkedin_unipile_invite", ("invite", "handle", "cancel")),
    "linkedin_unipile_post": ("linkedin_unipile_publish", ("create", "comment", "react")),
}

#: Ce qui reste au lecteur.
OPS_LUES = {
    **{f"{c}_chat": ["list", "read"] for c in CANAUX},
    "linkedin_unipile_chat": ["list", "read", "attendees", "contacts"],
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
        asyncio.run(m.call_tool("whatsapp_chat", {"op": "sned"}))
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

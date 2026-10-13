"""Google Chat et Teams : lecture et envoi dans des outils DIFFÉRENTS (#1231, décision
d'Alexis du 13/10/2026 — même découpage, même transition, même date que #1189).

Un client MCP (claude.ai, ChatGPT) autorise des OUTILS, pas des opérations : « toujours
autoriser » `chat_message` ou `teams_message` pour lire autorisait aussi d'y poster en
ton nom. Ce que ce fichier tient — la partie « transition » part tout entière au
retrait :

- les écrivains `chat_send` et `teams_send` déclarent `ENVOI` et vivent sous le
  connecteur de leur lecteur (même activation, même compte opéré `_account=`) ;
- jusqu'au 10/12/2026 inclus, l'ancienne op fait EXACTEMENT ce que fait l'écrivain :
  mêmes appels au client, même réponse, mêmes refus avant toute résolution de compte ;
- sa réponse le DIT (`deprecation_warning`), la description du lecteur le dit en une
  ligne, en tête ;
- tant qu'il poste, le lecteur reste déclaré `ENVOI`, jamais `readOnlyHint` ;
- la garde datée : à partir du 11/12/2026, ce banc rougit tant qu'une op dépréciée est
  encore servie.
"""
from __future__ import annotations

import asyncio
import datetime
import types
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP

from oto_mcp import call_axes, deprecations, error_taxonomy
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import chat as C
from oto_mcp.tools import teams as T
from oto_mcp.tools.lecture import ENVOI, en_lecture

#: lecteur → (écrivain, ops déplacées). La table de la décision, écrite une fois.
DECOUPAGE = {
    "chat_message": ("chat_send", ("send",)),
    "teams_message": ("teams_send", ("post", "reply")),
}

#: Un appel complet par op dépréciée (arguments de l'ancien appel, sans `op`).
_ARGS = {
    ("chat_message", "send"): [{"space": "spaces/A", "text": "bonjour"},
                               {"user": "marie@exemple.fr", "text": "bonjour",
                                "account": "a@exemple.fr"}],
    ("teams_message", "post"): [{"team_id": "t1", "channel_id": "ch", "body": "**ok**"},
                                {"chat_id": "c1", "body": "ok", "full": True}],
    ("teams_message", "reply"): [{"team_id": "t1", "channel_id": "ch",
                                  "message_id": "1700", "body": "vu"}],
}

CAS = [(lecteur, op, args) for (lecteur, op), liste in sorted(_ARGS.items())
       for args in liste]


def _monte() -> FastMCP:
    m = FastMCP("t")
    C.register(m)
    T.register(m)
    return m


@pytest.fixture
def m(clients):
    """Monté APRÈS les faux clients : `teams.register` lie `TeamsClient` à son appel."""
    return _monte()


@pytest.fixture
def clients(monkeypatch):
    """Les faux clients Google Chat et Teams, et la trace de chaque résolution de
    compte (Chat : `account` ; Teams : le jeton demandé)."""
    import oto.tools.microsoft as coeur

    etat = types.SimpleNamespace(chat=MagicMock(), teams=MagicMock(), comptes=[])
    for nom in ("send", "send_dm"):
        getattr(etat.chat, nom).return_value = {"name": f"spaces/A/messages/{nom}"}
    for nom in ("post_channel_message", "post_chat_message", "reply_channel_message"):
        getattr(etat.teams, nom).return_value = {"id": nom, "body": {"content": "x"}}
    monkeypatch.setattr(C, "_client_for_user",
                        lambda account=None: (etat.comptes.append(("chat", account)),
                                              etat.chat)[1])
    monkeypatch.setattr(coeur, "TeamsClient",
                        lambda jeton: (etat.comptes.append(("teams", jeton)),
                                       etat.teams)[1])
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for",
                        lambda sub, service, renew=False: "AT")
    return etat


def _appel(m, nom, args) -> dict:
    return asyncio.run(m.call_tool(nom, args)).structured_content


def _args_ecrivain(lecteur: str, op: str, args: dict) -> dict:
    """Les arguments du nouvel appel : les mêmes, avec `op` sauf pour `chat_send`."""
    return dict(args) if lecteur == "chat_message" else {"op": op, **args}


# ── Le découpage (tient au-delà du retrait) ─────────────────────────────────

@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_declare_qu_il_agit_chez_un_tiers(m, lecteur):
    a = asyncio.run(m.get_tool(DECOUPAGE[lecteur][0])).annotations
    assert a == ENVOI
    assert a.readOnlyHint is False and a.destructiveHint is True
    assert a.openWorldHint is True


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_vit_sous_le_connecteur_de_son_lecteur(lecteur):
    """Même namespace ⟹ même activation, même ACL, même sélection, et le même axe
    `_account=` : poster sous un AUTRE compte que la lecture serait pire qu'avant."""
    ecrivain = DECOUPAGE[lecteur][0]
    assert namespace_of(ecrivain) == namespace_of(lecteur)
    assert call_axes.accepts_account_axis(ecrivain) == call_axes.accepts_account_axis(
        lecteur), ecrivain


# ── La transition (part au retrait) ─────────────────────────────────────────

@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_le_lecteur_reste_declare_envoi_pendant_la_transition(m, lecteur):
    """Il poste encore : `readOnlyHint` lui ouvrirait les recettes « pull » et
    l'« autoriser toujours » d'un client sans confirmation d'envoi."""
    t = asyncio.run(m.get_tool(lecteur))
    assert not en_lecture(t), lecteur
    assert t.annotations == ENVOI, lecteur


@pytest.mark.parametrize("lecteur,op,args", CAS)
def test_l_ancienne_op_fait_exactement_ce_que_fait_le_nouvel_outil(
        m, clients, lecteur, op, args):
    """Chemin RÉEL (validation FastMCP comprise) : les mêmes appels au client, dans le
    même ordre, sous le même compte, et la même réponse — plus l'avertissement."""
    ecrivain = DECOUPAGE[lecteur][0]
    ancien = _appel(m, lecteur, {"op": op, **args})
    trace_ancienne = (list(clients.chat.mock_calls), list(clients.teams.mock_calls),
                      list(clients.comptes))
    for c in (clients.chat, clients.teams):
        c.reset_mock(return_value=False)
    clients.comptes.clear()
    neuf = _appel(m, ecrivain, _args_ecrivain(lecteur, op, args))
    assert trace_ancienne == (clients.chat.mock_calls, clients.teams.mock_calls,
                              clients.comptes)
    assert trace_ancienne[2], "l'appel a bien résolu un compte"
    avis = ancien.pop("deprecation_warning")
    assert ancien == neuf
    assert f"`{ecrivain}" in avis and deprecations.date_retrait_ops_envoi() in avis


@pytest.mark.parametrize("lecteur,args,manque", [
    ("chat_message", {"op": "send", "text": "bonjour"}, "either"),
    ("chat_message", {"op": "send", "space": "spaces/A", "user": "m@exemple.fr",
                      "text": "bonjour"}, "either"),
    ("chat_message", {"op": "send", "space": "spaces/A"}, "text"),
    ("teams_message", {"op": "post", "chat_id": "c1"}, "body"),
    ("teams_message", {"op": "post", "body": "x"}, "exactly one"),
    ("teams_message", {"op": "reply", "team_id": "t1", "channel_id": "ch",
                       "body": "x"}, "message_id"),
    ("teams_message", {"op": "reply", "chat_id": "c1", "message_id": "1",
                       "body": "x"}, "no threads"),
])
def test_l_ancienne_op_passe_par_les_refus_du_nouvel_outil(m, clients, lecteur, args,
                                                          manque):
    """Un envoi incomplet est refusé AVANT toute résolution de compte — le refus de
    l'écrivain, pas une copie."""
    with pytest.raises(Exception) as e:
        _appel(m, lecteur, args)
    assert manque in error_taxonomy.classify(e.value).message
    assert clients.comptes == [], "aucun compte ne doit être résolu pour un envoi incomplet"


def test_l_avertissement_servi_a_l_agent(m, clients):
    """Texte exact des réponses."""
    out = _appel(m, "chat_message", {"op": "send", "space": "spaces/A", "text": "hi"})
    assert out["deprecation_warning"] == (
        'Déprécié : `chat_message(op="send")` est retiré le 10/12/2026. L\'appel a été '
        "servi par `chat_send` ; appelle désormais `chat_send(…)` avec les mêmes "
        "arguments, sans `op`.")
    out = _appel(m, "teams_message", {"op": "reply", "team_id": "t1", "channel_id": "ch",
                                      "message_id": "1", "body": "vu"})
    assert out["deprecation_warning"] == (
        'Déprécié : `teams_message(op="reply")` est retiré le 10/12/2026. L\'appel a été '
        'servi par `teams_send` ; appelle désormais `teams_send(op="reply", …)` avec les '
        "mêmes arguments.")


@pytest.mark.parametrize("lecteur,ligne", [
    ("chat_message",
     "⚠️ op=send est déprécié, utiliser `chat_send` ; retrait le 10/12/2026."),
    ("teams_message",
     "⚠️ op=post/reply est déprécié, utiliser `teams_send` ; retrait le 10/12/2026."),
])
def test_la_description_du_lecteur_le_dit_en_une_ligne_en_tete(m, lecteur, ligne):
    assert asyncio.run(m.get_tool(lecteur)).description.split("\n")[0] == ligne


# ── La garde datée ──────────────────────────────────────────────────────────

def _ops_depreciees_servies(m) -> list:
    """Les anciennes ops d'envoi encore au schéma d'un lecteur MONTÉ."""
    servies = []
    for lecteur, (_, ops) in sorted(DECOUPAGE.items()):
        enum = asyncio.run(m.get_tool(lecteur)).parameters["properties"]["op"]["enum"]
        servies += [f"{lecteur}(op={o})" for o in ops if o in enum]
    return servies


def _verdict(aujourdhui: datetime.date, servies: list) -> Optional[str]:
    if not servies or aujourdhui <= deprecations.RETRAIT_OPS_ENVOI:
        return None
    return (f"Le {aujourdhui:%d/%m/%Y}, après le retrait annoncé au "
            f"{deprecations.date_retrait_ops_envoi()}, ces ops d'envoi sont encore "
            f"servies par des lecteurs : {servies}. Retire-les (le lecteur repasse "
            "`LECTURE`, l'ancienne op est refusée en nommant l'écrivain — "
            "docs/alias-deprecies.md, #1231) ou décale RETRAIT_OPS_ENVOI en le "
            "décidant — mais ne sers plus une date dépassée.")


def test_les_ops_depreciees_ne_survivent_pas_a_leur_date(m):
    """La sonnerie du retrait : rougit à partir du 11/12/2026 tant qu'une op dépréciée
    est encore servie."""
    verdict = _verdict(datetime.date.today(), _ops_depreciees_servies(m))
    assert verdict is None, verdict


def test_la_garde_datee_sonne_le_lendemain_du_retrait(m):
    servies = _ops_depreciees_servies(m)
    assert len(servies) == sum(len(ops) for _, ops in DECOUPAGE.values())
    assert _verdict(datetime.date(2026, 12, 10), servies) is None
    assert _verdict(datetime.date(2026, 12, 11), servies) is not None
    assert _verdict(datetime.date(2026, 12, 11), []) is None
    assert deprecations.RETRAIT_OPS_ENVOI == datetime.date(2026, 12, 10)

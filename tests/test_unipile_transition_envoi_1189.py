"""La transition de #1189 : jusqu'au 10/12/2026 inclus, l'ancienne op d'envoi d'un
lecteur marche encore (décision d'Alexis du 12/10/2026) — pour `linkedin_unipile_post` et
`linkedin_unipile_network` seulement depuis le 21/10/2026 : les messageries sont passées
à la paire `<ns>` / `<ns>_act`, sans transition (`tests/test_convention_act.py`).

Ce que ce fichier tient — et il part tout entier au retrait :

- l'ancienne op fait EXACTEMENT ce que fait le nouvel outil : mêmes appels au client,
  mêmes refus, avant toute résolution de clé ;
- sa réponse le DIT (`deprecation_warning` : l'outil à appeler, la date) ;
- la description du lecteur le dit en une ligne, en tête ;
- tant qu'il envoie, le lecteur reste déclaré `ENVOI` — jamais `readOnlyHint` : un
  client qui le croirait en lecture seule enverrait sans demander ;
- la garde datée : à partir du 11/12/2026, ce banc rougit tant qu'une op dépréciée est
  encore servie.
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP

from oto_mcp import deprecations, error_taxonomy
from oto_mcp.tools import unipile as U
from oto_mcp.tools.lecture import ENVOI, en_lecture

from test_unipile_lecture_envoi_1189 import DECOUPAGE, EN_TRANSITION

#: Les méthodes du client qu'un envoi peut toucher : chacune rend un dict, comme
#: l'oto-core réel.
_METHODES = ("create_post", "comment_post", "react_post", "send_invitation",
             "handle_invitation", "cancel_invitation")

#: Un appel complet par op dépréciée : (lecteur, op) → arguments de l'ancien appel.
_ARGS = {
    ("linkedin_unipile_post", "create"): {"text": "hello"},
    ("linkedin_unipile_post", "comment"): {"post_id": "urn:li:1", "text": "bravo"},
    ("linkedin_unipile_post", "react"): {"post_id": "urn:li:1", "value": "PRAISE"},
    ("linkedin_unipile_network", "invite"): {"provider_id": "ACoAA1", "message": "hi"},
    ("linkedin_unipile_network", "handle"): {"invitation_id": "i1",
                                             "shared_secret": "s1",
                                             "action": "decline"},
    ("linkedin_unipile_network", "cancel"): {"invitation_id": "i1"},
}

CAS = [(lecteur, op) for lecteur in EN_TRANSITION for op in DECOUPAGE[lecteur][1]]


def _monte() -> FastMCP:
    m = FastMCP("t")
    U.register(m)
    return m


@pytest.fixture
def m():
    return _monte()


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    for nom in _METHODES:
        getattr(inst, nom).return_value = {"object": nom}
    appels = []
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: (appels.append(a), inst)[1])
    inst.appels = appels
    return inst


def _appel(m, nom, args) -> dict:
    return asyncio.run(m.call_tool(nom, args)).structured_content


@pytest.mark.parametrize("lecteur", EN_TRANSITION)
def test_le_lecteur_reste_declare_envoi_pendant_la_transition(m, lecteur):
    """Il envoie encore : `readOnlyHint` lui ouvrirait les recettes « pull » et
    l'« autoriser toujours » d'un client sans confirmation d'envoi."""
    t = asyncio.run(m.get_tool(lecteur))
    assert not en_lecture(t), lecteur
    assert t.annotations == ENVOI, lecteur


@pytest.mark.parametrize("lecteur,op", CAS)
def test_l_ancienne_op_fait_exactement_ce_que_fait_le_nouvel_outil(
        m, client, lecteur, op):
    """Chemin RÉEL (validation FastMCP comprise) : les mêmes appels au client, dans le
    même ordre, et la même réponse — plus l'avertissement."""
    ecrivain = DECOUPAGE[lecteur][0]
    args = _ARGS[(lecteur, op)]
    ancien = _appel(m, lecteur, {"op": op, **args})
    appels_ancien, compte_ancien = list(client.mock_calls), list(client.appels)
    client.reset_mock()
    client.appels.clear()
    neuf = _appel(m, ecrivain, {"op": op, **args})
    assert appels_ancien == client.mock_calls
    assert compte_ancien == client.appels
    avis = ancien.pop("deprecation_warning")
    assert ancien == neuf
    assert f"`{ecrivain}" in avis and deprecations.date_retrait_ops_envoi() in avis


@pytest.mark.parametrize("lecteur,args,manque", [
    ("linkedin_unipile_post", {"op": "comment", "text": "bravo"}, "post_id"),
    ("linkedin_unipile_network", {"op": "handle", "invitation_id": "i1"},
     "shared_secret"),
])
def test_l_ancienne_op_passe_par_les_refus_du_nouvel_outil(m, client, lecteur, args,
                                                          manque):
    """Un envoi incomplet est refusé AVANT toute résolution de clé — le refus de
    l'écrivain, pas une copie."""
    with pytest.raises(Exception) as e:
        _appel(m, lecteur, args)
    assert manque in error_taxonomy.classify(e.value).message
    assert client.appels == [], "aucune clé ne doit être résolue pour un envoi incomplet"


def test_la_network_refuse_une_action_que_l_ecrivain_refuse(m, client):
    """`action` a le type de l'écrivain : une valeur qu'il refuse ne passe pas par
    l'ancien outil."""
    with pytest.raises(Exception):
        _appel(m, "linkedin_unipile_network",
               {"op": "handle", "invitation_id": "i1", "shared_secret": "s1",
                "action": "maybe"})
    assert client.appels == []


def test_l_avertissement_servi_a_l_agent(m, client):
    """Texte exact de la réponse de `linkedin_unipile_network(op="cancel")`."""
    out = _appel(m, "linkedin_unipile_network", {"op": "cancel", "invitation_id": "i1"})
    assert out["deprecation_warning"] == (
        'Déprécié : `linkedin_unipile_network(op="cancel")` est retiré le 10/12/2026. '
        "L'appel a été servi par `linkedin_unipile_invite` ; appelle désormais "
        '`linkedin_unipile_invite(op="cancel", …)` avec les mêmes arguments.')


@pytest.mark.parametrize("lecteur,ligne", [
    ("linkedin_unipile_network",
     "⚠️ op=invite/handle/cancel est déprécié, utiliser `linkedin_unipile_invite` ; "
     "retrait le 10/12/2026."),
    ("linkedin_unipile_post",
     "⚠️ op=create/comment/react est déprécié, utiliser `linkedin_unipile_publish` ; "
     "retrait le 10/12/2026."),
])
def test_la_description_du_lecteur_le_dit_en_une_ligne_en_tete(m, lecteur, ligne):
    assert asyncio.run(m.get_tool(lecteur)).description.split("\n")[0] == ligne


# ── La garde datée ──────────────────────────────────────────────────────────

def _ops_depreciees_servies(m) -> list:
    """Les anciennes ops d'envoi encore au schéma d'un lecteur MONTÉ."""
    servies = []
    for lecteur in EN_TRANSITION:
        ops = DECOUPAGE[lecteur][1]
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
            "docs/unipile.md §Lecture et envoi séparés) ou décale RETRAIT_OPS_ENVOI en "
            "le décidant — mais ne sers plus une date dépassée.")


def test_les_ops_depreciees_ne_survivent_pas_a_leur_date(m):
    """La sonnerie du retrait : rougit à partir du 11/12/2026 tant qu'une op dépréciée
    est encore servie."""
    verdict = _verdict(datetime.date.today(), _ops_depreciees_servies(m))
    assert verdict is None, verdict


def test_la_garde_datee_sonne_le_lendemain_du_retrait(m):
    servies = _ops_depreciees_servies(m)
    assert len(servies) == sum(len(DECOUPAGE[lecteur][1]) for lecteur in EN_TRANSITION)
    assert _verdict(datetime.date(2026, 12, 10), servies) is None
    assert _verdict(datetime.date(2026, 12, 11), servies) is not None
    assert _verdict(datetime.date(2026, 12, 11), []) is None
    assert deprecations.RETRAIT_OPS_ENVOI == datetime.date(2026, 12, 10)

"""`lemlist_create_lead` : les variables du lead partent en texte, et aucune n'écrase
un champ du lead.

L'OpenAPI de lemlist (`POST /campaigns/{campaignId}/leads/`, developer.lemlist.com, lue
le 2026-10-22) type les clés en plus des champs du lead `additionalProperties: string`,
et sa page le redit : « Values are stored as text ». `custom_variables` était fusionné
tel quel dans le corps : un nombre partait en nombre, et une variable nommée `email`
remplaçait en silence l'argument `email` du même appel.

Doublure du client : on lit le lead qu'il reçoit, aucun appel réel à lemlist."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError


class _FauxClient:
    envoye = None

    def __init__(self, api_key=None):
        pass

    def create_lead(self, campaign_id, lead, **flags):
        _FauxClient.envoye = dict(lead)
        return {"_id": "lea_1", "campaignId": campaign_id, **lead}


def _creer(**kw):
    from fastmcp import FastMCP
    from oto_mcp.tools import lemlist

    _FauxClient.envoye = None
    m = FastMCP("t")
    with patch("oto_mcp.access.resolve_api_key", return_value=("k", False)), \
            patch("oto.tools.lemlist.LemlistClient", _FauxClient):
        lemlist.register(m)
        fn = asyncio.run(m.get_tool("lemlist_create_lead")).fn
        return fn(campaign_id="cam_1", **kw)


def test_chaque_variable_part_en_texte():
    _creer(email="a@b.co", custom_variables={
        "effectif": 42, "ca": 1.5, "client": True, "villes": ["Lyon", "Paris"], "poste": "CTO"})
    assert _FauxClient.envoye == {
        "email": "a@b.co", "effectif": "42", "ca": "1.5", "client": "true",
        "villes": "Lyon, Paris", "poste": "CTO"}


@pytest.mark.parametrize("valeur", [{"a": 1}, None, [{"a": 1}]])
def test_une_variable_sans_forme_texte_est_refusee_avant_l_envoi(valeur):
    with pytest.raises(McpError) as exc:
        _creer(email="a@b.co", custom_variables={"poste": "CTO", "detail": valeur})
    assert exc.value.error.data["code"] == "lemlist_custom_variable_value"
    assert exc.value.error.data["keys"] == ["detail"]
    assert _FauxClient.envoye is None


@pytest.mark.parametrize("cle", ["email", "firstName", "contactOwner"])
def test_une_variable_qui_porte_le_nom_d_un_champ_est_refusee(cle):
    with pytest.raises(McpError) as exc:
        _creer(email="a@b.co", first_name="Marie", custom_variables={cle: "x@y.co"})
    assert exc.value.error.data["code"] == "lemlist_custom_variable_collision"
    assert exc.value.error.data["keys"] == [cle]
    assert _FauxClient.envoye is None


def test_le_champ_absent_de_l_appel_n_ouvre_pas_la_porte():
    """Même sans `email` en argument, `custom_variables={"email": …}` reste refusé :
    le champ a son argument, la variable ne le remplace pas."""
    with pytest.raises(McpError) as exc:
        _creer(linkedin_url="https://www.example.test/in/m", custom_variables={"email": "x@y.co"})
    assert exc.value.error.data["code"] == "lemlist_custom_variable_collision"

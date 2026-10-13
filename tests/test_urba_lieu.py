"""`urba_lieu` — les règles d'urbanisme d'une PARCELLE, d'une adresse ou d'une géométrie.

`urba_zonage` interroge un point : une servitude qui ne touche qu'un coin du terrain
lui échappe. `urba_lieu` passe par le client `UrbanismeClient` d'oto-core (GPU, cadastre
et géocodeur de l'IGN), qui interroge le contour de la parcelle. Ce banc tient le
câblage — un argument transmis tel quel, un refus de l'amont ou une adresse introuvable
rendus en clair à l'agent — sans réseau : le client est doublé sur le PACKAGE, que
l'outil lit à l'appel.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


def _outil():
    from fastmcp import FastMCP
    from oto_mcp.tools import urba as U

    m = FastMCP("banc-urba-lieu")
    U.register(m)
    return asyncio.run(m.get_tool("urba_lieu"))


@pytest.fixture
def client(monkeypatch):
    import oto.tools.urbanisme as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "UrbanismeClient", lambda **kw: inst)
    return inst


def test_les_arguments_partent_tels_quels(client):
    client.report.return_value = {"location": {"queried": "parcel"}, "notes": []}
    out = _outil().fn(idu="35238000AC0680", sup_categories=["AC1"])
    client.report.assert_called_once_with(address=None, idu="35238000AC0680", geom=None,
                                          on_parcel=True, sup_categories=["AC1"])
    assert out == {"location": {"queried": "parcel"}, "notes": []}


def test_une_adresse_introuvable_est_dite(client):
    from oto.tools.urbanisme import UrbanismeNotFound

    client.report.side_effect = UrbanismeNotFound("address not found: 'nulle part'")
    with pytest.raises(McpError, match="address not found"):
        _outil().fn(address="nulle part")


def test_un_appel_mal_forme_est_refuse_en_clair(client):
    client.report.side_effect = ValueError("give exactly one of address, idu or geom")
    with pytest.raises(McpError, match="exactly one"):
        _outil().fn(address="1 rue X", idu="35238000AC0680")


def test_un_refus_de_l_amont_porte_son_statut(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.report.side_effect = UpstreamHTTPError(400, {"message": "geom invalide"},
                                                  service="urbanisme")
    with pytest.raises(McpError, match="HTTP 400"):
        _outil().fn(geom={"type": "Point", "coordinates": [-1.68, 48.11]})


def test_la_description_dit_qu_un_vide_ne_prouve_rien():
    doc = _outil().description or ""
    assert "does NOT prove absence" in doc and "EXACTLY ONE" in doc

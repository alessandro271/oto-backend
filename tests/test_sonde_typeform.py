"""La sonde de connexion Typeform. Couvre `auth` et le scope `forms:read`.

`GET /forms?page_size=1` : la lecture la moins chère, sans effet de bord, sur
l'hôte de la région posée — une sonde qui viserait toujours les US dirait
« non autorisé » pour un jeton du data center `eu2`, parfaitement valide.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import typeform as T


def _fields(secret: str, **autres) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai pack/unpack empêche le drift sonde↔schéma."""
    return credentials_store.unpack_secret(
        "typeform", credentials_store.pack_secret("typeform", {"key": secret, **autres}))


class _FauxClient:
    def __init__(self, leve=None):
        self._leve = leve
        self.appels = []

    def list_forms(self, **kw):
        self.appels.append(kw)
        if self._leve:
            raise self._leve
        return {"total_items": 0, "page_count": 0, "items": []}


def _brancher(monkeypatch, client):
    import oto.tools.typeform as pkg

    def _construire(**kw):
        client.construit = kw
        return client

    monkeypatch.setattr(pkg, "TypeformClient", _construire)
    return client


def test_la_sonde_est_enregistree():
    from fastmcp import FastMCP
    T.register(FastMCP("t"))
    assert cv.supports("typeform")


def test_un_jeton_valide_ne_leve_pas(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    T._verify(_fields("tfp_k"))
    assert cli.appels == [{"page_size": 1}]
    assert cli.construit == {"access_token": "tfp_k", "region": "us"}


def test_la_sonde_suit_la_region_posee(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    T._verify(_fields("tfp_k", region="eu2"))
    assert cli.construit == {"access_token": "tfp_k", "region": "eu2"}


@pytest.mark.parametrize("status", [401, 403])
def test_un_refus_dauthentification_se_classe_non_autorise(monkeypatch, status):
    from oto.tools.common import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        leve=UpstreamHTTPError(status, {"code": "AUTHENTICATION_FAILED"}, service="typeform")))
    with pytest.raises(UpstreamHTTPError) as e:
        T._verify(_fields("tfp_k"))
    assert cv.classer(e.value) == cv.UNAUTHORIZED


def test_une_erreur_serveur_ne_se_classe_pas_non_autorise(monkeypatch):
    from oto.tools.common import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        leve=UpstreamHTTPError(500, "boom", service="typeform")))
    with pytest.raises(UpstreamHTTPError) as e:
        T._verify(_fields("tfp_k"))
    assert cv.classer(e.value) == cv.UNKNOWN

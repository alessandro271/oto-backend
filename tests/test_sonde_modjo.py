"""La sonde de connexion Modjo. Couvre la clé.

`GET /tags` (un seul élément) : une lecture sans donnée d'appel ni effet de bord —
c'est aussi la sonde que déclare la description du connecteur dans la bibliothèque.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import modjo as M
from oto_mcp.tools import modjo_socle as S


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai pack/unpack empêche le drift sonde↔schéma."""
    return credentials_store.unpack_secret(
        "modjo", credentials_store.pack_secret("modjo", {"key": secret}))


class _FauxClient:
    def __init__(self, leve=None):
        self._leve = leve
        self.appels = 0

    def probe(self):
        self.appels += 1
        if self._leve:
            raise self._leve
        return {"data": [], "pagination": {"page": 1, "size": 1, "total": 0}}


def _brancher(monkeypatch, client):
    import oto.tools.modjo as pkg

    def _construire(**kw):
        client.construit = kw
        return client

    monkeypatch.setattr(pkg, "ModjoClient", _construire)
    return client


def test_la_sonde_est_enregistree():
    from fastmcp import FastMCP
    M.register(FastMCP("t"))
    assert cv.supports("modjo")


def test_une_cle_valide_ne_leve_pas(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    S._verify(_fields("mj_k"))
    assert cli.appels == 1
    assert cli.construit == {"api_key": "mj_k"}


def test_une_cle_refusee_se_classe_non_autorisee(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(leve=UpstreamHTTPError(401, {"code": "invalid_api_key"})))
    with pytest.raises(UpstreamHTTPError) as e:
        S._verify(_fields("mj_k"))
    assert cv.classer(e.value) == cv.UNAUTHORIZED


def test_une_erreur_serveur_ne_se_classe_pas_non_autorisee(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(leve=UpstreamHTTPError(503, {"code": "internal_error"})))
    with pytest.raises(UpstreamHTTPError) as e:
        S._verify(_fields("mj_k"))
    assert cv.classer(e.value) == cv.UNKNOWN

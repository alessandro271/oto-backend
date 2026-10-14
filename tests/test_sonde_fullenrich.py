"""La sonde de connexion FullEnrich — otomata-tech/oto#69. Couvre `auth+quota`.

`GET /api/v1/account/credits` — v1, PAS le v2 du reste du client (deux
préfixes de version distincts). Le solde (`balance`) distingue une clé morte
d'un compte à sec.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import fullenrich as FE


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("fullenrich", secret)


class _FauxResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _brancher(monkeypatch, body=None, status=200, boom=None):
    import requests

    def _get(url, headers=None, timeout=None):
        if boom:
            raise boom
        assert url == FE._CREDITS_URL
        assert headers["Authorization"] == "Bearer k"
        return _FauxResponse(body, status)
    monkeypatch.setattr(requests, "get", _get)


def test_un_solde_disponible_est_rendu(monkeypatch):
    _brancher(monkeypatch, {"balance": 42})
    out = FE._verify(_fields("k"))
    assert out["quota"]["restant"] == 42


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    _brancher(monkeypatch, {"balance": 0})
    with pytest.raises(cv.QuotaEpuise) as e:
        FE._verify(_fields("k"))
    assert cv.classer(e.value) == cv.NO_QUOTA


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, {"error": "invalid key"}, status=401)
    with pytest.raises(Exception):
        FE._verify(_fields("k"))


def test_un_solde_ILLISIBLE_ne_fabrique_pas_de_quota_mais_echoue(monkeypatch):
    _brancher(monkeypatch, {"foo": "bar"})
    with pytest.raises(RuntimeError, match="without a readable credit balance"):
        FE._verify(_fields("k"))


@pytest.mark.parametrize("brut, attendu", [
    (42, 42), (5404.75, 5404.75), ("5404.75", 5404.75), ("5404", 5404), (0.5, 0.5)])
def test_un_solde_DECIMAL_ou_en_texte_est_lu_tel_quel(monkeypatch, brut, attendu):
    """Signal #1442 (14/10/2026) : FullEnrich rend un solde décimal (5404.75), que le
    contrôle « entier seulement » lisait illisible. Rien n'est tronqué : 0.5 crédit
    n'est pas un compte à sec."""
    _brancher(monkeypatch, {"balance": brut})
    assert FE._verify(_fields("k"))["quota"]["restant"] == attendu


@pytest.mark.parametrize("brut", [0, 0.0, "0", -3.5])
def test_un_solde_nul_ou_negatif_reste_un_compte_a_sec(monkeypatch, brut):
    _brancher(monkeypatch, {"balance": brut})
    with pytest.raises(cv.QuotaEpuise):
        FE._verify(_fields("k"))


@pytest.mark.parametrize("body", [
    {}, {"balance": None}, {"balance": True}, {"balance": ""}, {"balance": "abc"},
    {"balance": "nan"}, {"balance": float("nan")}, {"balance": "inf"}, {"balance": [5]}])
def test_un_solde_absent_ou_non_numerique_reste_illisible(monkeypatch, body):
    """NaN surtout : il compare faux à `<= 0` et passerait pour un compte approvisionné."""
    _brancher(monkeypatch, body)
    with pytest.raises(RuntimeError, match="without a readable credit balance"):
        FE._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth_quota():
    from fastmcp import FastMCP

    FE.register(FastMCP("t"))
    assert cv.supports("fullenrich")
    assert cv.couverture("fullenrich") == cv.AUTH_QUOTA

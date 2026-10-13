"""La confiance d'adresse explicite (#1232) : `client_trace.pick_ip`.

Un en-tête d'adresse (`CF-Connecting-IP`, `X-Forwarded-For`) est écrit par qui veut. Il
ne se lit que si le pair TCP est un proxy DÉCLARÉ (`OTO_PROXIES_DE_CONFIANCE`) ; sans
réglage, c'est le pair TCP. Adresses des plages de documentation (RFC 5737)."""
from __future__ import annotations

import pytest

from oto_mcp import client_trace as ct

CLIENT, AUTRE = "203.0.113.7", "198.51.100.9"


def _poser(monkeypatch, valeur):
    if valeur is None:
        monkeypatch.delenv("OTO_PROXIES_DE_CONFIANCE", raising=False)
    else:
        monkeypatch.setenv("OTO_PROXIES_DE_CONFIANCE", valeur)
    monkeypatch.setattr(ct, "_RESEAUX", None)


@pytest.mark.parametrize("valeur", [None, "", " , "])
def test_sans_reglage_le_pair_tcp_et_rien_d_autre(monkeypatch, valeur):
    _poser(monkeypatch, valeur)
    assert ct.pick_ip(CLIENT, AUTRE, "127.0.0.1") == "127.0.0.1"
    assert ct.pick_ip(CLIENT, AUTRE, None) is None


def test_un_pair_hors_des_reseaux_declares_ne_dicte_pas_son_adresse(monkeypatch):
    _poser(monkeypatch, "127.0.0.1/32,::1/128")
    assert ct.pick_ip(CLIENT, AUTRE, "192.0.2.50") == "192.0.2.50"
    assert ct.pick_ip(CLIENT, AUTRE, "testclient") == "testclient"


def test_derriere_un_proxy_declare_cf_connecting_ip_d_abord(monkeypatch):
    _poser(monkeypatch, "127.0.0.1/32,::1/128")
    assert ct.pick_ip(f" {CLIENT} ", AUTRE, "127.0.0.1") == CLIENT
    assert ct.pick_ip(CLIENT, None, "::1") == CLIENT


def test_x_forwarded_for_se_lit_depuis_la_droite(monkeypatch):
    """Chaque relais ajoute en queue : à gauche du dernier relais de confiance, c'est
    le client qui a écrit."""
    _poser(monkeypatch, "127.0.0.1/32,10.0.0.0/8")
    assert ct.pick_ip(None, f"192.0.2.66, {AUTRE}", "127.0.0.1") == AUTRE
    assert ct.pick_ip("", f"192.0.2.66, {AUTRE}, 10.1.2.3", "127.0.0.1") == AUTRE
    # toute la chaîne est de confiance : son premier saut
    assert ct.pick_ip(None, "10.0.0.2, 10.0.0.1", "127.0.0.1") == "10.0.0.2"
    # aucun en-tête utile : le pair
    assert ct.pick_ip(None, " , ", "127.0.0.1") == "127.0.0.1"


def test_un_pair_ipv4_mappe_en_ipv6_est_reconnu(monkeypatch):
    _poser(monkeypatch, "127.0.0.1")
    assert ct.pick_ip(CLIENT, None, "::ffff:127.0.0.1") == CLIENT


@pytest.mark.parametrize("valeur", ["127.0.0.1,localhost", "10.0.0.0/33", "abc"])
def test_un_reglage_illisible_refuse_le_demarrage(monkeypatch, valeur):
    _poser(monkeypatch, valeur)
    with pytest.raises(ct.ProxiesDeConfianceInvalides, match="OTO_PROXIES_DE_CONFIANCE"):
        ct.verifier_configuration()


def test_le_reglage_se_lit_une_fois(monkeypatch):
    _poser(monkeypatch, "127.0.0.1")
    ct.verifier_configuration()
    monkeypatch.setenv("OTO_PROXIES_DE_CONFIANCE", "pas une adresse")
    assert ct.pick_ip(CLIENT, None, "127.0.0.1") == CLIENT


def test_ip_de_requete_lit_une_requete_starlette(monkeypatch):
    from starlette.requests import Request
    _poser(monkeypatch, "127.0.0.1")
    req = Request({"type": "http", "headers": [(b"cf-connecting-ip", CLIENT.encode())],
                   "client": ("127.0.0.1", 5000)})
    assert ct.ip_de_requete(req) == CLIENT
    req = Request({"type": "http", "headers": [(b"cf-connecting-ip", CLIENT.encode())],
                   "client": None})
    assert ct.ip_de_requete(req) is None

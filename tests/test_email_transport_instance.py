"""Le transport des mails de plateforme se DÉCLARE : relais mailer ou API TEM de l'instance.

`email.transport_plateforme` choisit entre `OTO_MAILER_URL` et le trio `OTO_TEM_*` ; aucun,
les deux, ou TEM à moitié LÈVE (#968), au démarrage comme à l'envoi. Le transport TEM
réutilise `send_via_scaleway_tem` et garde les garanties de `_send` : CR/LF neutralisés,
`cc`, `reply_to`, expéditeur `OTO_MAIL_FROM`.
"""
from __future__ import annotations

import sys
import types

import pytest

from oto_mcp import email as E, identite_instance

_TEM = {"OTO_TEM_SECRET_KEY": "cle-tem", "OTO_TEM_PROJECT_ID": "projet-tem",
        "OTO_TEM_REGION": "fr-par"}


def _fake_httpx(monkeypatch, status: int = 200) -> list[dict]:
    envois: list[dict] = []

    class _Resp:
        status_code = status
        text = "refus"

    def post(url, headers=None, json=None, timeout=None):
        envois.append({"url": url, "headers": headers, "json": json})
        return _Resp()
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(post=post))
    return envois


@pytest.fixture
def tem(monkeypatch):
    """Une instance qui envoie par son API TEM, sans relais mailer."""
    monkeypatch.delenv("OTO_MAILER_URL", raising=False)
    monkeypatch.delenv("OTO_MAILER_SEND_BEARER", raising=False)
    for nom, valeur in _TEM.items():
        monkeypatch.setenv(nom, valeur)
    return monkeypatch


# ── le choix du transport ────────────────────────────────────────────────────

def test_le_relais_seul_est_le_transport(monkeypatch):
    for nom in _TEM:
        monkeypatch.delenv(nom, raising=False)
    assert E.transport_plateforme() == "https://mailer.oto.zone/api/send"


def test_tem_seul_est_le_transport(tem):
    t = E.transport_plateforme()
    assert isinstance(t, E.TemInstance)
    assert (t.secret_key, t.project_id, t.region) == ("cle-tem", "projet-tem", "fr-par")
    assert "cle-tem" not in repr(t), "la clé ne s'imprime pas dans une trace"


def test_un_relais_vide_ne_compte_pas(tem):
    tem.setenv("OTO_MAILER_URL", "")
    assert isinstance(E.transport_plateforme(), E.TemInstance)


def test_sans_transport_l_envoi_leve(tem):
    for nom in _TEM:
        tem.delenv(nom)
    envois = _fake_httpx(tem)
    with pytest.raises(RuntimeError, match="aucun transport.*OTO_MAILER_URL.*OTO_TEM_SECRET_KEY"):
        E._send("a@x.fr", "s", "<p>x</p>")
    assert envois == []


def test_les_deux_transports_levent(tem):
    tem.setenv("OTO_MAILER_URL", "https://relais.test/api/send")
    tem.setenv("OTO_MAILER_SEND_BEARER", "tok")
    envois = _fake_httpx(tem)
    with pytest.raises(RuntimeError, match="ambigu"):
        E._send("a@x.fr", "s", "<p>x</p>")
    assert envois == []


@pytest.mark.parametrize("manquante", sorted(_TEM))
def test_tem_a_moitie_leve_en_nommant_ce_qui_manque(tem, manquante):
    tem.delenv(manquante)
    with pytest.raises(RuntimeError, match=f"à moitié.*{manquante}"):
        E.transport_plateforme()


def test_le_demarrage_rejoue_le_choix(tem):
    for nom in _TEM:
        tem.delenv(nom)
    with pytest.raises(identite_instance.IdentiteNonDeclaree, match="OTO_TEM_SECRET_KEY"):
        identite_instance.verifier()


# ── l'envoi par TEM ──────────────────────────────────────────────────────────

def test_l_envoi_tem_part_par_l_api_avec_les_identifiants_de_l_instance(tem):
    tem.setenv("OTO_MAIL_FROM", "Tiers <bonjour@tiers.test>")
    envois = _fake_httpx(tem)
    assert E._send("a@x.fr", "sujet", "<p>x</p>", reply_to="r@x.fr", cc=["b@x.fr"]) is True
    (envoi,) = envois
    assert envoi["url"] == ("https://api.scaleway.com/transactional-email/v1alpha1/"
                            "regions/fr-par/emails")
    assert envoi["headers"] == {"X-Auth-Token": "cle-tem"}
    p = envoi["json"]
    assert p["project_id"] == "projet-tem"
    assert p["from"] == {"email": "bonjour@tiers.test", "name": "Tiers"}
    assert p["to"] == [{"email": "a@x.fr"}] and p["subject"] == "sujet"
    assert p["cc"] == [{"email": "b@x.fr"}]
    assert p["additional_headers"] == [{"key": "Reply-To", "value": "r@x.fr"}]


def test_l_envoi_tem_ne_demande_pas_de_bearer(tem):
    envois = _fake_httpx(tem)
    assert E._send("a@x.fr", "s", "<p>x</p>") is True
    assert envois[0]["json"]["from"] == {"email": "oto@otomata.tech", "name": "Oto"}


def test_l_envoi_tem_neutralise_crlf_sur_chaque_en_tete(tem):
    envois = _fake_httpx(tem)
    assert E._send("a@x.fr\r\nBcc: v@evil.com", "objet\r\nBcc: v@evil.com", "<p>x</p>",
                   from_email="Nom\r\nX: 1 <f@x.fr>", reply_to="r@x.fr\nX-Injected: 1",
                   cc=["c@x.fr\r\nBcc: v@evil.com"])
    p = envois[0]["json"]
    valeurs = [p["to"][0]["email"], p["subject"], p["from"]["email"], p["from"].get("name", ""),
               p["additional_headers"][0]["value"], p["cc"][0]["email"]]
    for v in valeurs:
        assert "\r" not in v and "\n" not in v, v


def test_un_expediteur_sans_adresse_leve(tem):
    envois = _fake_httpx(tem)
    with pytest.raises(RuntimeError, match="OTO_MAIL_FROM"):
        E._send("a@x.fr", "s", "<p>x</p>", from_email="Pas une adresse")
    assert envois == []


def test_un_refus_de_scaleway_rend_false(tem):
    _fake_httpx(tem, status=400)
    assert E._send("a@x.fr", "s", "<p>x</p>") is False


def test_un_relais_declare_par_l_appelant_ignore_le_transport_de_l_instance(tem):
    envois = _fake_httpx(tem)
    assert E._send("a@x.fr", "s", "<p>x</p>", mailer_url="https://relais.tenant.test",
                   bearer="jeton-du-tenant") is True
    assert envois[0]["url"] == "https://relais.tenant.test"
    assert envois[0]["headers"] == {"Authorization": "Bearer jeton-du-tenant"}


def test_les_gabarits_partent_par_tem(tem):
    """Un mail composé (même chemin que les invitations et notifications) part par TEM."""
    envois = _fake_httpx(tem)
    assert E.send_composed_email("a@x.fr", "sujet", "bonjour") is True
    assert envois[0]["url"].startswith("https://api.scaleway.com/transactional-email/")

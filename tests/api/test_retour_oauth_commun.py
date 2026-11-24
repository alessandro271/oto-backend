"""Le retour d'un consentement, le même pour tous les connecteurs (oto-backend#670, #1908).

Une route par fournisseur (`api/oauth_returns.py`), un seul code (`auth/flow.py`). Ces
bancs jouent la VRAIE route de chacun des retours déclarés, avec un state lu remplacé
(chaque module garde ses propres bancs de state) :

- state illisible : `connect=error`, sur la carte par défaut du fournisseur ;
- refus du fournisseur : `connect=denied`, sur la carte du state ;
- autre erreur, ou ni code ni motif : `connect=error`, sur la carte du state ;
- un code : `connect=confirm`, code et state dans le FRAGMENT — rien n'est échangé ni
  rangé ni consommé ; le front termine (`POST /api/me/connectors/{name}/complete`).
"""
from __future__ import annotations

import dataclasses
import urllib.parse

import pytest

from _retour_oauth import rappel
from oto_mcp.auth import flow as oauth_flow

#: Le chemin de chaque retour, enregistré octet pour octet chez son fournisseur.
CHEMINS = {
    "custom_api": "/api/custom-api/oauth/callback",
    "google": "/api/google/oauth/callback",
    "hubspot": "/api/hubspot/oauth/callback",
    "instagram_meta": "/api/instagram_meta/oauth/callback",
    "jefacture": "/api/jefacture/oauth/callback",
    "linkedin_ads": "/api/linkedin_ads/oauth/callback",
    "meta_ads": "/api/meta_ads/oauth/callback",
    "microsoft": "/api/microsoft/oauth/callback",
    "salesforce": "/api/salesforce/oauth/callback",
    "slack": "/api/slack/oauth/callback",
    "ubersuggest": "/api/ubersuggest/oauth/callback",
    "wordpress": "/api/wordpress/connect/callback",
    "zoho": "/api/zoho/oauth/callback",
}


def test_chaque_fournisseur_declare_son_retour_sur_son_chemin():
    retours = oauth_flow.load_returns()
    assert {a: r.callback_path for a, r in retours.items()} == CHEMINS


def _carte(retour) -> str:
    """Une carte servie par ce retour, autre que la carte par défaut quand il y en a."""
    cartes = tuple(retour.cards())
    return next((c for c in cartes if c != retour.default_card), cartes[0])


@pytest.fixture
def lu(monkeypatch):
    """Chaque retour lit un state fixe, porté par une de ses cartes ; tout échange ou
    rangement fait rougir le banc, et la consommation aussi."""
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    def _jamais(*a, **k):
        raise AssertionError("le callback ne doit ni échanger, ni ranger, ni consommer")

    monkeypatch.setattr(oauth_flow, "consume_state", _jamais)
    for audience, r in oauth_flow.load_returns().items():
        etat = oauth_flow.ReturnState("sub-1", 7, "", _carte(r), "jti-1", None)
        monkeypatch.setitem(oauth_flow._RETURNS, audience, dataclasses.replace(
            r, read=lambda s, etat=etat: etat if s == "s" else None, finish=_jamais,
            side_return=None))
    return oauth_flow.load_returns()


def _q(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


@pytest.mark.parametrize("audience", sorted(CHEMINS))
def test_un_code_part_au_front_dans_le_fragment(lu, audience):
    url = rappel(CHEMINS[audience], {"code": "c/1", "state": "s", "password": "p"})
    q = _q(url)
    assert q["connect"] == ["confirm"] and q["connector"] == [_carte(lu[audience])]
    assert "code" not in q and "state" not in q, "jamais en query : le fragment seul"
    remis = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment)
    assert remis["state"] == ["s"] and remis["code"]
    if audience != "wordpress":
        assert remis["code"] == ["c/1"]


@pytest.mark.parametrize("audience", sorted(set(CHEMINS) - {"wordpress"}))
def test_un_refus_chez_le_fournisseur_revient_en_denied(lu, audience):
    q = _q(rappel(CHEMINS[audience], {"error": "access_denied", "state": "s"}))
    assert q["connect"] == ["denied"]
    assert q["connector"] == [_carte(lu[audience])], "la carte du state, pas le défaut"


def test_un_refus_chez_wordpress_revient_en_denied(lu):
    q = _q(rappel(CHEMINS["wordpress"], {"success": "false", "state": "s"}))
    assert q["connect"] == ["denied"]


@pytest.mark.parametrize("audience", sorted(CHEMINS))
@pytest.mark.parametrize("query", [{"error": "server_error"}, {}])
def test_une_autre_erreur_ou_un_code_absent_reste_une_panne(lu, audience, query):
    q = _q(rappel(CHEMINS[audience], {**query, "state": "s"}))
    assert q["connect"] == ["error"] and q["connector"] == [_carte(lu[audience])]


@pytest.mark.parametrize("audience", sorted(CHEMINS))
def test_un_state_illisible_revient_en_error_sur_la_route_des_connecteurs(
        lu, audience, monkeypatch):
    """Sans state lisible, rien ne dit d'où vient la personne : le défaut commun
    (`/connectors` du dashboard), jamais un `denied` sur la foi d'un paramètre."""
    monkeypatch.setenv("OTO_APP_URL", "https://dashboard.example")
    url = rappel(CHEMINS[audience], {"error": "access_denied", "state": "bad"})
    lieu = urllib.parse.urlsplit(url)
    assert (lieu.scheme, lieu.netloc, lieu.path) == ("https", "dashboard.example",
                                                     "/connectors")
    assert _q(url)["connect"] == ["error"]
    assert _q(url)["connector"] == [lu[audience].default_card]


# --- la complétion : les refus communs ----------------------------------------

def _retour_fictif(monkeypatch, fin=lambda data, code: {"account": "a", "scope": "member"}):
    vus: set = set()
    monkeypatch.setattr(oauth_flow, "consume_state",
                        lambda aud, d: not ((aud, d["jti"]) in vus or vus.add((aud, d["jti"]))))
    etat = oauth_flow.ReturnState("sub-1", 7, "", "carte", "jti-1", {"x": 1})
    return oauth_flow.OAuthReturn(
        audience="fictif", callback_path="/x", label="Fictif", default_card="carte",
        read=lambda s: etat if s == "s" else None, finish=fin, cards=lambda: ("carte",))


def test_la_completion_range_pour_le_porteur_une_seule_fois(monkeypatch):
    r = _retour_fictif(monkeypatch)
    assert oauth_flow.complete(r, "sub-1", "c", "s", card="carte") == {
        "ok": True, "connector": "carte", "account": "a", "scope": "member"}
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        oauth_flow.complete(r, "sub-1", "c", "s", card="carte")
    assert (e.value.status, e.value.code) == (409, "state_used")


@pytest.mark.parametrize("sub,state,carte,attendu", [
    ("sub-2", "s", "carte", (403, "state_not_yours")),
    (None, "s", "carte", (403, "state_not_yours")),
    ("sub-1", "bad", "carte", (400, "invalid_state")),
    ("sub-1", "s", "autre", (400, "invalid_state")),
])
def test_la_completion_refuse_sans_rien_consommer(monkeypatch, sub, state, carte, attendu):
    def _jamais(data, code):
        raise AssertionError("rien ne s'échange")
    r = _retour_fictif(monkeypatch, _jamais)
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        oauth_flow.complete(r, sub, "c", state, card=carte)
    assert (e.value.status, e.value.code) == attendu
    # Le state reste utilisable par la personne qui l'a démarré.
    r2 = dataclasses.replace(r, finish=lambda d, c: {"scope": "member"})
    assert oauth_flow.complete(r2, "sub-1", "c", "s", card="carte")["ok"] is True


def test_un_echec_du_connecteur_devient_un_refus_nomme(monkeypatch):
    def _boom(data, code):
        raise RuntimeError("diagnostic interne")
    r = _retour_fictif(monkeypatch, _boom)
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        oauth_flow.complete(r, "sub-1", "c", "s")
    assert (e.value.status, e.value.code) == (502, "exchange_failed")
    assert "diagnostic" not in e.value.message


def test_un_refus_nomme_du_connecteur_passe_tel_quel(monkeypatch):
    def _refus(data, code):
        raise oauth_flow.CompletionRefused(409, "org_mismatch", "m", {"consented_org": "X"})
    r = _retour_fictif(monkeypatch, _refus)
    with pytest.raises(oauth_flow.CompletionRefused) as e:
        oauth_flow.complete(r, "sub-1", "c", "s")
    assert (e.value.code, e.value.details) == ("org_mismatch", {"consented_org": "X"})


def test_la_route_generique_nomme_une_carte_inconnue(monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    code, out = call("me.connector_complete", path_params={"name": "inconnue"},
                     body={"code": "c", "state": "s"})
    assert (code, out["error"]) == (404, "unknown_connector")


@pytest.mark.parametrize("audience", sorted(CHEMINS))
def test_chaque_carte_se_termine_par_la_route_generique(audience):
    retour = oauth_flow.load_returns()[audience]
    for carte in retour.cards():
        assert oauth_flow.return_for_card(carte) is retour

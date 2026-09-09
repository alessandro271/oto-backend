"""Les verbes du consentement OAuth per-user de google, en capacités : mêmes chemins,
mêmes codes.

Dix routes `/api/{atlassian,folkmcp,google}/oauth*` ont quitté `api/atlassian.py`,
`api/folk.py` et `api/datastore.py` pour `capabilities/federated_oauth.py`
(27/08). Les **callbacks** restent écrits à la main : le fournisseur y redirige le
navigateur (302, sans auth), l'adaptateur REST authentifie toujours et répond en JSON.

⚠️ **`atlassian` et `folkmcp` sont partis le 2026-09-09** avec la fédération MCP
(ADR 0069) ; il ne reste ici que `google`, qui n'a jamais été fédéré. Ce banc ne
perd rien de ce qu'il verrouillait pour lui — les mêmes assertions, sur les mêmes
quatre routes vivantes.

Ce que ce fichier garde :

- **Les champs racine de `google/oauth/status` décrivent le compte PAR DÉFAUT**, pas
  l'union des comptes — héritage du mono-compte. Sans défaut posé, ils sont vides alors
  que `connected` est vrai : c'est cohérent, et c'est contre-intuitif.
- **`DELETE /api/google/oauth` SANS paramètre révoque TOUT.** `account: null` dans la
  réponse veut dire « tous », pas « aucun ».
- **Le 500 `oauth_misconfigured:`** garde son format exact, espace compris : il signale
  une app OAuth mal configurée côté PLATEFORME, pas une erreur de l'appelant.
- **Aucune clé `me.federation.{atlassian,folkmcp}.*` ne revient** : leur retour serait
  une régression silencieuse du retrait, pas une amélioration à fêter.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import db
from oto_mcp.auth import google as google_oauth
from oto_mcp.capabilities import federated_oauth as fo

_COMPTES = [
    {"google_email": "a@x.io", "is_default": True, "scopes": "s1 s2",
     "granted_at": "2026-08-01"},
    {"google_email": "b@x.io", "is_default": False, "scopes": None,
     "granted_at": "2026-08-02"},
]


@pytest.fixture()
def socle(monkeypatch):
    vus: list = []
    monkeypatch.setattr(google_oauth, "build_auth_url",
                        lambda sub: f"https://google/auth?s={sub}")
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: list(_COMPTES))
    monkeypatch.setattr(google_oauth, "revoke",
                        lambda sub, account=None: vus.append(("revoke", sub, account)))
    monkeypatch.setattr(fo.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(fo.db, "set_default_google_account",
                        lambda sub, oid, acc: vus.append(("defaut", sub, oid, acc)) or True)
    return vus


# --- Ce que le retrait de la fédération a emporté ---------------------------

@pytest.mark.parametrize("nom", ["atlassian", "folkmcp"])
@pytest.mark.parametrize("verbe", ["start", "status", "disconnect"])
def test_aucune_capacite_de_federation_ne_subsiste(nom, verbe):
    """La fédération MCP est retirée (2026-09-09, ADR 0069) : plus AUCUNE clé
    `me.federation.{atlassian,folkmcp}.*` au registre. `.start` et `.disconnect`
    étaient déjà sortis le 04/09 (mesurés à 0 appel/30j, oto-dashboard#125) ;
    `.status` part avec le mécanisme. Un retour de l'une d'elles serait une
    régression silencieuse du retrait — c'est exactement ce que ce test attrape."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    assert f"me.federation.{nom}.{verbe}" not in {c.key for c in CAPABILITIES}


def test_les_quatre_verbes_google_sont_TOUS_restes():
    """Le pendant du test ci-dessus, et le vrai enjeu du lot : retirer la fédération
    ne devait RIEN retirer à google. Les quatre clés servies sont là."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cles = {c.key for c in CAPABILITIES}
    for verbe in ("start", "status", "revoke", "set_default"):
        assert f"me.federation.google.{verbe}" in cles, verbe


# --- Google, multi-compte ---------------------------------------------------

def test_les_champs_racine_decrivent_le_compte_par_defaut(monkeypatch, socle):
    """⚠️ PAS l'union des comptes : héritage du temps où Google était mono-compte. Un
    intégrateur qui lit `scopes` à la racine lit ceux du défaut, pas les siens."""
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.status")
    assert code == 200
    assert out["connected"] is True
    assert out["granted_at"] == "2026-08-01" and out["scopes"] == ["s1", "s2"]
    assert [a["email"] for a in out["accounts"]] == ["a@x.io", "b@x.io"]
    # Le compte sans `scopes` en base rend une liste VIDE, jamais null.
    assert out["accounts"][1]["scopes"] == []
    assert set(out) == set(fo.GoogleStatus.model_fields)


def test_des_comptes_SANS_defaut_laissent_la_racine_vide(monkeypatch, socle):
    """`connected: true` avec `granted_at: null` est cohérent, et surprenant : il y a des
    comptes, aucun n'est élu par défaut. Le figer évite qu'on « corrige » la racine en y
    mettant le premier compte venu."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: [
        {"google_email": "b@x.io", "is_default": False, "scopes": "s1",
         "granted_at": "2026-08-02"}])
    _, out = call("me.federation.google.status")
    assert out["connected"] is True
    assert out["granted_at"] is None and out["scopes"] == []


def test_aucun_compte(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: [])
    _, out = call("me.federation.google.status")
    assert out == {"connected": False, "granted_at": None, "scopes": [], "accounts": []}


def test_une_app_oauth_mal_configuree_est_un_500_qui_NOMME_la_cause(monkeypatch, socle):
    """Le format exact, espace compris : le code machine porte la cause. C'est une panne
    de PLATEFORME, pas une erreur de l'appelant — d'où le 5xx."""
    stub_authz(monkeypatch)

    def _boum(sub):
        raise RuntimeError("GOOGLE_CLIENT_ID absent")

    monkeypatch.setattr(google_oauth, "build_auth_url", _boum)
    code, out = call("me.federation.google.start")
    assert code == 500
    assert out["error"] == "oauth_misconfigured: GOOGLE_CLIENT_ID absent"


@pytest.mark.parametrize("query,attendu", [
    (b"", None),                       # SANS paramètre = TOUS les comptes
    (b"account=", None),               # vide = idem
    (b"account=a%40x.io", "a@x.io"),
])
def test_revoquer_sans_compte_revoque_TOUT(monkeypatch, socle, query, attendu):
    """⚠️ `account: null` dans la réponse veut dire « tous », pas « aucun ». C'est le
    geste le plus destructeur de cette surface et rien ne le signalait."""
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.revoke", query=query)
    assert (code, out) == (200, {"ok": True, "account": attendu})
    assert socle[0] == ("revoke", "u-1", attendu)


def test_elire_un_defaut_le_normalise(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.set_default", body={"account": " a@x.io "})
    assert (code, out) == (200, {"ok": True, "default": "a@x.io"})
    assert socle[0] == ("defaut", "u-1", 35, "a@x.io")


@pytest.mark.parametrize("corps", [{}, {"account": ""}, {"account": "   "}])
def test_un_defaut_sans_compte_est_refuse(monkeypatch, socle, corps):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.set_default", body=corps)
    assert code == 400 and out["error"] == "missing_account"
    assert socle == []


def test_un_compte_inconnu_ou_hors_org_rend_le_meme_404(monkeypatch, socle):
    """Les deux causes — compte absent, ou aucune org de contexte — rendent
    `unknown_account`. C'est ce qui est servi ; le figer évite qu'un « affinage » invente
    un second code que personne ne lit."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(fo.db, "set_default_google_account", lambda s, o, a: False)
    assert call("me.federation.google.set_default",
                body={"account": "z@x.io"})[1]["error"] == "unknown_account"
    monkeypatch.setattr(fo.access, "current_org", lambda sub: None)
    code, out = call("me.federation.google.set_default", body={"account": "a@x.io"})
    assert code == 404 and out["error"] == "unknown_account"


# --- Ce qui CHANGE pour un appelant -----------------------------------------

def test_un_champ_inconnu_est_desormais_refuse(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.revoke", query=b"compte=a%40x.io")
    assert code == 400
    assert out["error"] == "unknown_fields" and "compte" in out["detail"]

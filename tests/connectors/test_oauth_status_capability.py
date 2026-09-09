"""`me.connector_status` / `me.connector_disconnect` (oto-dashboard#125, items 2/3) —
le couple symétrique de `me.connector_connect` : un chemin fixe qui ne nomme pas le
connecteur, pour lire l'état d'un consentement OAuth et le révoquer.

⚠️ **Ils couvraient TROIS connecteurs jusqu'au 2026-09-09** (atlassian, folkmcp,
google) ; les deux premiers sont partis avec la fédération MCP (ADR 0069). Le chemin
reste GÉNÉRIQUE pour le seul google — c'est le contrat que le dashboard appelle, et
il ne se replie pas parce que la liste a rétréci.

Ce que ce fichier MORD :

- **Contrainte 1** (bloquante, arbitrage du 04/09/2026) — `me.connector_status` ne doit
  JAMAIS interroger `auth.google` en lecture : son état vient d'`access.status_for`, la
  MÊME source que `/api/me`. Le test patche le lecteur `auth.*` pour qu'il LÈVE si on
  l'appelle, et vérifie que la capacité répond quand même — la preuve qu'elle ne passe
  jamais par là.
- **Contrainte 2** (décision d'Alexis) — `me.connector_disconnect` appelle bien la
  fonction de révocation déjà en production (`google_oauth.revoke`) et rend
  `disconnected` fidèlement, en un seul appel, idempotent sur le suivant.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.auth import google as google_oauth
from oto_mcp.capabilities.connectors import oauth_status
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.connectors import flow_status


def _cap(key: str):
    return next(c for c in CAPABILITIES if c.key == key)


# --- Inventaire du registre --------------------------------------------------

def test_seul_google_est_couvert():
    """⚠️ La liste a RÉTRÉCI le 2026-09-09 : atlassian et folkmcp sont partis avec la
    fédération MCP (ADR 0069). Ce test est ce qui empêche l'un des deux de revenir
    par une déclaration oubliée."""
    assert set(flow_status.entries()) == {"google"}


def test_aucun_verbe_status_nest_cable_ici():
    """Contrainte 1 : ce registre ne branche QUE `disconnect` — `status` reste `None`,
    sinon `me.connector_status` aurait un second chemin pour diverger
    de `access.status_for`."""
    for nom, f in flow_status.entries().items():
        assert f.status is None, f"{nom} : un lecteur status est câblé — contrainte 1 violée"
        assert f.disconnect is not None, f"{nom} : aucun verbe disconnect câblé"


def test_les_chemins_sont_fixes_et_ne_collisionnent_pas():
    cap_status = _cap("me.connector_status")
    cap_disc = _cap("me.connector_disconnect")
    assert (cap_status.rest.verb, cap_status.rest.path) == (
        "GET", "/api/me/connectors/{name}/oauth-status")
    assert (cap_disc.rest.verb, cap_disc.rest.path) == (
        "DELETE", "/api/me/connectors/{name}/oauth")
    # Chemins DÉJÀ servis sous ce préfixe (connect/select/pause/unselect) : aucun des
    # deux nouveaux ne coïncide avec l'existant, y compris le DELETE nu (unselect).
    existants = {(c.rest.verb, c.rest.path) for c in CAPABILITIES
                if c.rest is not None and not isinstance(c.rest, tuple)
                and c.rest.path.startswith("/api/me/connectors/{name}")
                and c.key not in ("me.connector_status", "me.connector_disconnect")}
    assert existants == {
        ("POST", "/api/me/connectors/{name}/connect"),
        ("POST", "/api/me/connectors/{name}/select"),
        ("POST", "/api/me/connectors/{name}/pause"),
        ("DELETE", "/api/me/connectors/{name}"),
        ("POST", "/api/me/connectors/{name}/session/start"),
        ("POST", "/api/me/connectors/{name}/session/finalize"),
    }
    assert (cap_status.rest.verb, cap_status.rest.path) not in existants
    assert (cap_disc.rest.verb, cap_disc.rest.path) not in existants
    for authz_name in ("me.connector_status", "me.connector_disconnect"):
        assert _cap(authz_name).mcp is None       # pas de face agent sur ce lot


def test_un_connecteur_non_couvert_est_refuse(monkeypatch):
    stub_authz(monkeypatch)
    code, out = call("me.connector_status", path_params={"name": "zoho"})
    assert code == 400 and out["error"] == "no_oauth_status"
    code, out = call("me.connector_disconnect", path_params={"name": "zoho"})
    assert code == 400 and out["error"] == "no_oauth_status"


# --- Contrainte 1 : `me.connector_status` ne crée pas une seconde vérité ---------

def test_le_statut_ne_touche_JAMAIS_un_module_auth(monkeypatch):
    """MORD sur la contrainte 1 : le lecteur `auth.*` est patché pour LEVER s'il est
    appelé — `me.connector_status` doit répondre quand même, en lisant UNIQUEMENT
    `access.status_for`."""
    nom = "google"
    stub_authz(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError(
            f"{nom} : un module auth.* a été appelé — la source doit être "
            "access.status_for seul (contrainte 1)")

    monkeypatch.setattr(google_oauth, "list_accounts", _boom)
    monkeypatch.setattr(oauth_status.access, "status_for", lambda sub: {
        "providers": {
            nom: {"mode": "user", "user_key_configured": True,
                 "session_set_at": "2026-09-01T00:00:00Z",
                 "health_ko": True, "health_reason": "refresh_token_expired"},
        }})
    code, out = call("me.connector_status", path_params={"name": nom})
    assert code == 200
    assert out == {"connected": True, "set_at": "2026-09-01T00:00:00Z",
                   "health_ko": True, "health_reason": "refresh_token_expired"}


def test_jamais_connecte_rend_connected_false_et_health_null(monkeypatch):
    stub_authz(monkeypatch)
    monkeypatch.setattr(oauth_status.access, "status_for", lambda sub: {"providers": {}})
    code, out = call("me.connector_status", path_params={"name": "google"})
    assert code == 200
    assert out == {"connected": False, "set_at": None,
                   "health_ko": None, "health_reason": None}
    assert set(out) == set(oauth_status.ConnectorOAuthStatus.model_fields)


# --- Contrainte 2 : déconnexion irréversible, un seul appel, idempotente --------

def test_la_deconnexion_google_revoque_TOUS_les_comptes_en_un_appel(monkeypatch):
    """Pas de double étape (contrainte 2) : `account=None` — le même comportement,
    au champ près, que `federated_oauth._google_revoke` appelé sans paramètre."""
    stub_authz(monkeypatch)
    vus: list = []
    monkeypatch.setattr(google_oauth, "revoke",
                        lambda sub, account=None: vus.append((sub, account)))
    code, out = call("me.connector_disconnect", path_params={"name": "google"})
    assert (code, out) == (200, {"ok": True, "disconnected": True})
    assert vus == [("u-1", None)]
    # Idempotent : un second appel réussit encore, sans état intermédiaire.
    code, out = call("me.connector_disconnect", path_params={"name": "google"})
    assert (code, out) == (200, {"ok": True, "disconnected": True})
    assert vus == [("u-1", None), ("u-1", None)]

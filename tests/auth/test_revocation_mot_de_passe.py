"""Un mot de passe changé chez Logto ferme ce qu'il ouvrait — `POST /api/logto/webhook`.
Un changement de double authentification ferme la même chose (`auth/revocation_2fa.py`).

On joue la VRAIE route (Starlette) sur des corps signés comme Logto les signe (HMAC-SHA256
du corps brut, digest hexadécimal dans `logto-signature-sha-256`) ; la base et la
Management API sont simulées. Un cas par refus, un cas par événement, et la fermeture
Logto vérifiée appel par appel (route, paramètre `revokeGrantsTarget=all`). Puis la
double authentification : sur le webhook, à la remise à zéro par un opérateur (qui ne
passe par aucun webhook), et la lecture des facteurs chez Logto.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import tenancy
from oto_mcp.api import logto_webhook
from oto_mcp.auth import annuaire, revocation_2fa, revocation_mot_de_passe as rmp
from oto_mcp.capabilities import users_admin
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CLE = "cle-de-signature-de-test"
SUB = "u-logto-1"
#: Les facteurs du compte tels que la Management API les rend, posés il y a longtemps.
FACTEURS = [
    {"id": "totp-1", "type": "Totp", "createdAt": "2026-01-02T10:00:00.000Z"},
    {"id": "codes-1", "type": "BackupCode", "createdAt": "2026-01-02T10:01:00.000Z",
     "remainCodes": [{"code": "x", "usedAt": None}]},
]


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _signe(corps: bytes, cle: str = CLE) -> str:
    return hmac.new(cle.encode(), corps, hashlib.sha256).hexdigest()


def _reinit_pwd() -> dict:
    return {"hookId": "h1", "event": "PostResetPassword", "createdAt": _maintenant(),
            "interactionEvent": "ForgotPassword", "userId": SUB,
            "user": {"id": SUB}}


def _pwd_par_management() -> dict:
    return {"hookId": "h1", "event": "User.Data.Updated", "createdAt": _maintenant(),
            "path": f"/users/{SUB}/password", "method": "PATCH", "status": 200,
            "matchedRoute": "/users/:userId/password", "params": {"userId": SUB},
            "data": {"id": SUB}}


class _Vus(list):
    """Le relevé des gestes, avec l'état simulé de la double authentification."""


@pytest.fixture
def monde(monkeypatch):
    """La base et Logto simulés ; `vus` relève chaque geste dans l'ordre."""
    vus = _Vus()
    monkeypatch.setenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY", CLE)
    jetons = [{"id": 1, "parent_id": None}, {"id": 2, "parent_id": 1},
              {"id": 3, "parent_id": None}]
    monkeypatch.setattr(rmp.db, "list_api_tokens",
                        lambda sub, kinds=("user",), **kw:
                        vus.append(("list", sub, kinds)) or list(jetons))
    monkeypatch.setattr(rmp.db, "revoke_api_token",
                        lambda sub, tid, revoked_by, reason, parent_id=None:
                        vus.append(("revoke", sub, tid, revoked_by, reason)) or True)
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions",
                        lambda sub: vus.append(("logto", sub))
                        or {"sessions": 2, "grants": 1})
    # La double authentification : les facteurs du moment chez Logto, et l'empreinte
    # posée — par défaut celle de ces mêmes facteurs (rien n'a changé).
    etat = {"facteurs": [dict(f) for f in FACTEURS],
            "posees": {SUB: revocation_2fa.empreinte(FACTEURS)}}
    monkeypatch.setattr(revocation_2fa.annuaire, "facteurs_2fa",
                        lambda sub: [dict(f) for f in etat["facteurs"]])
    monkeypatch.setattr(revocation_2fa.empreintes_2fa, "lire",
                        lambda sub: etat["posees"].get(sub))

    def _poser(sub, valeur):
        vus.append(("empreinte", sub))
        etat["posees"][sub] = valeur
    monkeypatch.setattr(revocation_2fa.empreintes_2fa, "poser", _poser)
    vus.etat = etat
    return vus


def _client() -> TestClient:
    return TestClient(Starlette(routes=logto_webhook.make_routes()),
                      raise_server_exceptions=False)


def _poste(evt, *, signature: "str | None | bool" = True, brut: bytes | None = None):
    corps = brut if brut is not None else json.dumps(evt).encode()
    entetes = {"content-type": "application/json"}
    if signature is True:
        entetes[rmp.ENTETE_SIGNATURE] = _signe(corps)
    elif signature:
        entetes[rmp.ENTETE_SIGNATURE] = signature
    return _client().post(logto_webhook.PATH, content=corps, headers=entetes)


# --- La signature --------------------------------------------------------------

def test_signature_valide_reinitialisation_revoque_et_ferme(monde):
    r = _poste(_reinit_pwd())
    assert r.status_code == 200, r.text
    assert r.json() == {"ignored": False, "event": "PostResetPassword",
                        "reason": "password_changed",
                        "revoked_tokens": 3, "closed_sessions": 2, "revoked_grants": 1}
    assert ("logto", SUB) in monde


def test_signature_fausse_refusee_sans_rien_toucher(monde):
    r = _poste(_reinit_pwd(), signature=_signe(b"autre corps"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_signature_d_une_autre_cle_refusee(monde):
    corps = json.dumps(_reinit_pwd()).encode()
    r = _poste(None, brut=corps, signature=_signe(corps, "autre-cle"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_signature_absente_refusee(monde):
    r = _poste(_reinit_pwd(), signature=None)
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_la_signature_porte_sur_les_octets_recus_pas_sur_le_json(monde):
    """Un espace de plus dans le corps signé ne change rien au JSON — et invalide la
    signature calculée sur l'autre forme : on juge les OCTETS."""
    evt = _reinit_pwd()
    signe_sur = json.dumps(evt).encode()
    envoye = json.dumps(evt, indent=1).encode()
    r = _poste(None, brut=envoye, signature=_signe(signe_sur))
    assert r.status_code == 401
    assert monde == []


def test_cle_absente_refuse_tout_meme_signe(monde, monkeypatch):
    monkeypatch.delenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY")
    r = _poste(_reinit_pwd())
    assert r.status_code == 503 and r.json()["error"] == "logto_webhook_not_configured"
    assert monde == []


def test_cle_vide_vaut_absente(monde, monkeypatch):
    monkeypatch.setenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY", "  ")
    r = _poste(_reinit_pwd(), signature=_signe(json.dumps(_reinit_pwd()).encode(), "  "))
    assert r.status_code == 503
    assert monde == []


# --- Le corps ------------------------------------------------------------------

def test_corps_illisible_mais_signe_refuse_nommement(monde):
    r = _poste(None, brut=b"{pas du json")
    assert r.status_code == 400 and r.json()["error"] == "invalid_event"
    assert monde == []


def test_corps_illisible_non_signe_dit_la_signature_d_abord(monde):
    r = _poste(None, brut=b"{pas du json", signature=None)
    assert r.status_code == 401


def test_livraison_ancienne_refusee(monde):
    evt = {**_reinit_pwd(), "createdAt": "2020-01-01T00:00:00.000Z"}
    r = _poste(evt)
    assert r.status_code == 400 and r.json()["error"] == "stale_event"
    assert monde == []


def test_livraison_sans_date_refusee(monde):
    evt = _reinit_pwd()
    del evt["createdAt"]
    r = _poste(evt)
    assert r.status_code == 400 and r.json()["error"] == "invalid_event"


# --- Les événements ------------------------------------------------------------

def test_changement_par_la_management_api_revoque(monde):
    r = _poste(_pwd_par_management())
    assert r.status_code == 200 and r.json()["revoked_tokens"] == 3, r.text
    assert ("logto", SUB) in monde


@pytest.mark.parametrize("evt", [
    # Une autre route de management : profil, données personnalisées, échéance du
    # mot de passe — pas un mot de passe changé.
    {"event": "User.Data.Updated", "method": "PATCH", "matchedRoute": "/users/:userId",
     "params": {"userId": SUB}, "data": {"id": SUB}},
    {"event": "User.Data.Updated", "method": "PATCH",
     "matchedRoute": "/users/:userId/password/expiration", "params": {"userId": SUB}},
    # L'Account API et l'Experience API : la livraison ne porte que l'entité, sans
    # route — un changement d'email ou de MFA y ressemble trait pour trait.
    {"event": "User.Data.Updated", "data": {"id": SUB}},
    {"event": "User.Created", "data": {"id": SUB}},
    {"event": "PostSignIn", "userId": SUB},
])
def test_evenement_qui_ne_dit_pas_un_mot_de_passe_change_est_ignore(monde, evt):
    """Facteurs de double authentification inchangés : rien n'est fermé."""
    r = _poste({**evt, "hookId": "h1", "createdAt": _maintenant()})
    assert r.status_code == 200 and r.json() == {"ignored": True, "event": evt["event"]}
    assert not [e for e in monde if e[0] != "empreinte"], \
        "un événement ignoré ne révoque rien et ne ferme rien chez Logto"


# --- La révocation des jetons --------------------------------------------------

def test_revocation_passe_par_le_geste_unique_avec_sa_trace(monde):
    _poste(_reinit_pwd())
    assert monde[0] == ("list", SUB, ("user", "delegation"))
    revoques = [e for e in monde if e[0] == "revoke"]
    assert [e[2] for e in revoques] == [2, 1, 3], "l'enfant avant son émetteur"
    assert {e[3] for e in revoques} == {rmp.REVOQUE_PAR}
    assert all("mot de passe" in e[4] for e in revoques)
    # Les jetons d'abord : si Logto tombe, ils sont déjà coupés.
    assert monde.index(("logto", SUB)) > monde.index(revoques[-1])


def test_logto_en_echec_rend_502_jetons_deja_coupes(monde, monkeypatch):
    def _tombe(sub):
        raise annuaire.FermetureInachevee("Logto injoignable")
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions", _tombe)
    r = _poste(_reinit_pwd())
    assert r.status_code == 502 and r.json()["error"] == "logto_sessions_not_closed"
    assert len([e for e in monde if e[0] == "revoke"]) == 3


def test_panne_de_notre_cote_rend_500_nomme(monde, monkeypatch):
    def _base_tombee(sub, **kw):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(rmp.db, "list_api_tokens", _base_tombee)
    r = _poste(_reinit_pwd())
    assert r.status_code == 500 and r.json()["error"] == "logto_webhook_failed"


# --- La fermeture chez Logto (Management API simulée) --------------------------

class _Rep:
    def __init__(self, statut: int, corps: dict | None = None):
        self.status_code, self._corps = statut, corps or {}

    def json(self):
        return self._corps


@pytest.fixture
def logto(monkeypatch):
    import requests
    appels: list = []
    reponses: dict = {
        ("GET", f"/api/users/{SUB}/sessions"): _Rep(200, {"sessions": [
            {"payload": {"uid": "s1"}}, {"payload": {"uid": "s2"}}]}),
        ("DELETE", f"/api/users/{SUB}/sessions/s1"): _Rep(204),
        ("DELETE", f"/api/users/{SUB}/sessions/s2"): _Rep(404),   # échue entre-temps
        ("GET", f"/api/users/{SUB}/grants"): _Rep(200, {"grants": [{"id": "g1"}]}),
        ("DELETE", f"/api/users/{SUB}/grants/g1"): _Rep(204),
    }

    def _request(methode, url, headers=None, timeout=None, params=None):
        chemin = url.removeprefix("https://annuaire.test")
        appels.append((methode, chemin, params, headers.get("Authorization"), timeout))
        return reponses[(methode, chemin)]

    monkeypatch.setenv("LOGTO_ENDPOINT", "https://annuaire.test")
    monkeypatch.setattr(annuaire, "_mgmt_token", lambda directory=None: "JETON-M2M")
    monkeypatch.setattr(tenancy, "require_primary_tenant", lambda sub, action: None)
    monkeypatch.setattr(requests, "request", _request)
    return appels, reponses


def test_fermeture_logto_sessions_avec_grants_puis_grants_restants(logto):
    appels, _ = logto
    assert annuaire.fermer_sessions(SUB) == {"sessions": 1, "grants": 1}
    assert [(a[0], a[1], a[2]) for a in appels] == [
        ("GET", f"/api/users/{SUB}/sessions", None),
        ("DELETE", f"/api/users/{SUB}/sessions/s1", {"revokeGrantsTarget": "all"}),
        ("DELETE", f"/api/users/{SUB}/sessions/s2", {"revokeGrantsTarget": "all"}),
        ("GET", f"/api/users/{SUB}/grants", None),
        ("DELETE", f"/api/users/{SUB}/grants/g1", None),
    ]
    assert {a[3] for a in appels} == {"Bearer JETON-M2M"}
    assert all(a[4] and a[4] <= annuaire._DELAI_REQUETE_S for a in appels)


def test_fermeture_logto_un_refus_leve_nomme(logto):
    _, reponses = logto
    reponses[("DELETE", f"/api/users/{SUB}/sessions/s1")] = _Rep(500)
    with pytest.raises(annuaire.FermetureInachevee):
        annuaire.fermer_sessions(SUB)


def test_fermeture_logto_sans_credential_leve_nomme(logto, monkeypatch):
    def _absent(directory=None):
        raise RuntimeError("credential de management absent")
    monkeypatch.setattr(annuaire, "_mgmt_token", _absent)
    with pytest.raises(annuaire.FermetureInachevee):
        annuaire.fermer_sessions(SUB)


def test_fermeture_logto_borne_son_propre_delai(logto, monkeypatch):
    """Une borne à SON niveau : passé `_DELAI_FERMETURE_S`, la suite n'est pas tentée
    et l'échec se dit."""
    horloge = iter([0.0, 0.0, annuaire._DELAI_FERMETURE_S + 1] + [999.0] * 10)
    monkeypatch.setattr(annuaire.time, "monotonic", lambda: next(horloge))
    appels, _ = logto
    with pytest.raises(annuaire.FermetureInachevee, match="délai"):
        annuaire.fermer_sessions(SUB)
    assert len(appels) == 1


def _profil() -> dict:
    """Ce que l'Account API envoie pour toute retouche du compte : l'entité, sans route."""
    return {"hookId": "h1", "event": "User.Data.Updated", "createdAt": _maintenant(),
            "data": {"id": SUB}}


def _fermes(monde) -> bool:
    return ("logto", SUB) in monde and any(e[0] == "revoke" for e in monde)


def _il_y_a(secondes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=secondes)).isoformat()


# --- La double authentification sur le webhook ----------------------------------

def test_facteur_ajoute_ferme_tout_puis_pose_l_empreinte(monde):
    monde.etat["facteurs"].append({"id": "cle-1", "type": "WebAuthn",
                                   "createdAt": _maintenant()})
    r = _poste(_profil())
    assert r.status_code == 200, r.text
    assert r.json() == {"ignored": False, "event": "User.Data.Updated",
                        "reason": "mfa_changed", "revoked_tokens": 3,
                        "closed_sessions": 2, "revoked_grants": 1}
    assert _fermes(monde)
    assert monde.index(("empreinte", SUB)) > monde.index(("logto", SUB)), \
        "l'empreinte neuve se pose après la fermeture"
    assert monde.etat["posees"][SUB] == revocation_2fa.empreinte(monde.etat["facteurs"])
    revoques = [e for e in monde if e[0] == "revoke"]
    assert all("double authentification" in e[4] for e in revoques)


def test_facteur_retire_ferme_tout(monde):
    monde.etat["facteurs"] = [f for f in monde.etat["facteurs"] if f["type"] != "Totp"]
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["reason"] == "mfa_changed"
    assert _fermes(monde)


def test_dernier_facteur_retire_ferme_tout(monde):
    monde.etat["facteurs"] = []
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["reason"] == "mfa_changed"
    assert _fermes(monde)


def test_codes_de_secours_regeneres_ferment_tout(monde):
    """Régénérer les codes de secours remplace le facteur : son id change."""
    for f in monde.etat["facteurs"]:
        if f["type"] == "BackupCode":
            f["id"] = "codes-2"
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["reason"] == "mfa_changed"
    assert _fermes(monde)


def test_retouche_de_profil_ne_ferme_rien(monde):
    """Un code de secours consommé, une date de dernier usage, un nom de clé : les
    mêmes facteurs, la même empreinte."""
    for f in monde.etat["facteurs"]:
        f["remainCodes"] = [{"code": "x", "usedAt": "2026-11-20T00:00:00.000Z"}]
        f["lastUsedAt"] = "2026-11-20T00:00:00.000Z"
        f["name"] = "renommé"
    r = _poste(_profil())
    assert r.status_code == 200 and r.json() == {"ignored": True,
                                                 "event": "User.Data.Updated"}
    assert not _fermes(monde) and not [e for e in monde if e[0] == "revoke"]


def test_empreinte_sans_facteur_puis_premier_facteur_ferme_tout(monde):
    monde.etat["posees"][SUB] = revocation_2fa.empreinte([])
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["reason"] == "mfa_changed"
    assert _fermes(monde)


def test_premier_passage_pose_sans_fermer(monde):
    monde.etat["posees"].clear()
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["ignored"] is True
    assert not _fermes(monde)
    assert monde.etat["posees"][SUB] == revocation_2fa.empreinte(FACTEURS)


def test_premier_passage_sans_facteur_pose_l_empreinte_vide(monde):
    monde.etat["posees"].clear()
    monde.etat["facteurs"] = []
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["ignored"] is True
    assert monde.etat["posees"][SUB] == revocation_2fa.empreinte([])


@pytest.mark.parametrize("forme", ["iso", "millisecondes"])
def test_premier_passage_avec_facteur_recent_ferme_tout(monde, forme):
    """Logto rend un instant en ISO 8601 ou en millisecondes."""
    cree = (_il_y_a(120) if forme == "iso" else
            int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000))
    monde.etat["posees"].clear()
    monde.etat["facteurs"].append({"id": "totp-2", "type": "Totp", "createdAt": cree})
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["reason"] == "mfa_changed"
    assert _fermes(monde)


def test_premier_passage_facteur_de_plus_d_une_heure_ne_ferme_rien(monde):
    monde.etat["posees"].clear()
    monde.etat["facteurs"].append({"id": "totp-2", "type": "Totp",
                                   "createdAt": _il_y_a(revocation_2fa.RECENT_S + 60)})
    r = _poste(_profil())
    assert r.status_code == 200 and r.json()["ignored"] is True
    assert not _fermes(monde)


def test_facteurs_illisibles_rendent_502_sans_rien_fermer_ni_poser(monde, monkeypatch):
    def _tombe(sub):
        raise annuaire.LectureInachevee("Logto injoignable")
    monkeypatch.setattr(revocation_2fa.annuaire, "facteurs_2fa", _tombe)
    r = _poste(_profil())
    assert r.status_code == 502 and r.json()["error"] == "logto_mfa_not_read"
    assert list(monde) == []


def test_fermeture_en_echec_ne_pose_pas_l_empreinte(monde, monkeypatch):
    """L'écart reste : la relivraison de Logto le retrouve et ferme."""
    def _tombe(sub):
        raise annuaire.FermetureInachevee("Logto injoignable")
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions", _tombe)
    avant = monde.etat["posees"][SUB]
    monde.etat["facteurs"].append({"id": "cle-1", "type": "WebAuthn",
                                   "createdAt": _maintenant()})
    r = _poste(_profil())
    assert r.status_code == 502 and r.json()["error"] == "logto_sessions_not_closed"
    assert monde.etat["posees"][SUB] == avant
    assert ("empreinte", SUB) not in monde


def test_livraison_rejouee_apres_fermeture_ne_ferme_plus(monde):
    monde.etat["facteurs"].append({"id": "cle-1", "type": "WebAuthn",
                                   "createdAt": _maintenant()})
    assert _poste(_profil()).json()["reason"] == "mfa_changed"
    monde.clear()
    r = _poste(_profil())
    assert r.json()["ignored"] is True and not _fermes(monde)


def test_mot_de_passe_change_ne_lit_pas_les_facteurs(monde, monkeypatch):
    def _interdit(sub):
        raise AssertionError("lecture des facteurs sur un mot de passe changé")
    monkeypatch.setattr(revocation_2fa.annuaire, "facteurs_2fa", _interdit)
    r = _poste({"hookId": "h1", "event": "PostResetPassword", "createdAt": _maintenant(),
                "userId": SUB})
    assert r.status_code == 200 and r.json()["reason"] == "password_changed"


# --- La remise à zéro par un opérateur ------------------------------------------

@pytest.fixture
def remise(monde, monkeypatch):
    monkeypatch.setattr(users_admin, "_resolve_target", lambda t: t)
    monkeypatch.setattr(users_admin.db, "get_user", lambda sub: {"sub": sub})
    monkeypatch.setattr(annuaire, "reset_user_mfa",
                        lambda sub: monde.append(("reset", sub)) or ["Totp", "BackupCode"])
    return monde


def test_remise_a_zero_ferme_tout_et_pose_l_empreinte_vide(remise):
    ctx = ResolvedCtx(sub="operateur-1")
    rendu = users_admin._reset_mfa(ctx, users_admin.ResetMfaInput(target=SUB))
    assert rendu == {"ok": True, "sub": SUB, "removed": ["Totp", "BackupCode"]}
    assert remise[0] == ("reset", SUB)
    assert _fermes(remise)
    revoques = [e for e in remise if e[0] == "revoke"]
    assert {e[3] for e in revoques} == {"operateur-1"}, "la trace nomme l'opérateur"
    assert remise.etat["posees"][SUB] == revocation_2fa.empreinte([])
    assert remise.index(("empreinte", SUB)) > remise.index(("logto", SUB))


def test_remise_a_zero_fermeture_en_echec_rend_502(remise, monkeypatch):
    def _tombe(sub):
        raise annuaire.FermetureInachevee("Logto injoignable")
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions", _tombe)
    with pytest.raises(AuthzDenied) as e:
        users_admin._reset_mfa(ResolvedCtx(sub="operateur-1"),
                               users_admin.ResetMfaInput(target=SUB))
    assert e.value.status == 502 and e.value.code == "logto_sessions_not_closed"


# --- La lecture des facteurs chez Logto (Management API simulée) -----------------

@pytest.fixture
def logto_facteurs(monkeypatch):
    import requests
    appels: list = []
    reponse = {"r": _Rep(200, FACTEURS)}

    def _get(url, headers=None, timeout=None):
        appels.append((url.removeprefix("https://annuaire.test"),
                       headers.get("Authorization"), timeout))
        return reponse["r"]

    monkeypatch.setenv("LOGTO_ENDPOINT", "https://annuaire.test")
    monkeypatch.setattr(annuaire, "_mgmt_token", lambda directory=None: "JETON-M2M")
    monkeypatch.setattr(tenancy, "require_primary_tenant", lambda sub, action: None)
    monkeypatch.setattr(requests, "get", _get)
    return appels, reponse


def test_lecture_des_facteurs_un_appel_borne(logto_facteurs):
    appels, _ = logto_facteurs
    assert annuaire.facteurs_2fa(SUB) == FACTEURS
    assert appels == [(f"/api/users/{SUB}/mfa-verifications", "Bearer JETON-M2M",
                       annuaire._DELAI_REQUETE_S)]


def test_lecture_des_facteurs_un_refus_leve_nomme(logto_facteurs):
    _, reponse = logto_facteurs
    reponse["r"] = _Rep(500)
    with pytest.raises(annuaire.LectureInachevee):
        annuaire.facteurs_2fa(SUB)


def test_lecture_des_facteurs_sans_credential_leve_nomme(logto_facteurs, monkeypatch):
    def _absent(directory=None):
        raise RuntimeError("credential de management absent")
    monkeypatch.setattr(annuaire, "_mgmt_token", _absent)
    with pytest.raises(annuaire.LectureInachevee):
        annuaire.facteurs_2fa(SUB)

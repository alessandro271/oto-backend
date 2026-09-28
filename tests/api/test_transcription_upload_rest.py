"""Dépôt DIRECT d'un audio à transcrire — `POST /api/me/projects/{id}/transcriptions/upload`.

Route écrite à la main (corps multipart, ADR 0009) : elle n'a aucune des garanties de
l'adaptateur de capacités, d'où ce banc. Le cœur (`capabilities.transcription.deposer`,
éprouvé par `tests/test_transcription.py`) est remplacé par un témoin : on tient ici
ce que la ROUTE ajoute.

1. **Les gardes AVANT la lecture du corps** : projet inconnu ou hors de l'org de
   consultation (404 non-disclosant, avant le 403), écriture refusée — un compte qui
   ne peut pas écrire ne fait rien téléverser.
2. **La borne pendant la lecture** (#562) : un audio au-delà du plafond est refusé
   (413), rien n'est déposé.
3. **Le formulaire** : `file` requis, `vocabulary` passé tel quel, `vocabulary_replace`
   strictement `true`|`false`.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp import access, db, ownership
from oto_mcp.api import base
from oto_mcp.api import transcription as route
from oto_mcp.capabilities import transcription as coeur
from oto_mcp.capabilities._types import AuthzDenied

PID = 42
CHEMIN = f"/api/me/projects/{PID}/transcriptions/upload"
AUDIO = b"ID3" + b"\x00" * 4096


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@projets.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture
def depots(monkeypatch):
    """Le projet existe, il est dans l'org de consultation, l'acteur y écrit ; le cœur
    enregistre ce qu'il reçoit."""
    monkeypatch.setattr("oto_mcp.account_suspension.refus", lambda sub: None)
    monkeypatch.setattr(base, "alias_drain_armed", lambda: False)
    monkeypatch.setattr(db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 3)
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: {"id": pid, "name": "Visite"})
    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: True)
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)
    journal: list[dict] = []

    def _deposer(sub, pid, lire, *, vocabulary=None, vocabulary_replace=False):
        f = lire()
        journal.append({"sub": sub, "pid": pid, "data": f.data, "filename": f.filename,
                        "mime": f.mime, "vocabulary": vocabulary,
                        "vocabulary_replace": vocabulary_replace})
        return {"job_id": 5, "status": "pending"}

    monkeypatch.setattr(coeur, "deposer", _deposer)
    return journal


@pytest.fixture
def client(depots):
    return TestClient(Starlette(routes=[
        Route("/api/me/projects/{project_id:int}/transcriptions/upload",
              base.bind(route.transcription_upload, verifier=_Verifier()), methods=["POST"]),
    ]))


def _audio():
    return {"file": ("reunion.ogg", AUDIO, "audio/ogg")}


def _auth(sub="u1") -> dict:
    return {"Authorization": f"Bearer {sub}"}


def test_un_audio_envoye_devient_un_travail(client, depots):
    r = client.post(CHEMIN, files=_audio(), headers=_auth(),
                    data={"vocabulary": "Voxtral, Otomata", "vocabulary_replace": "true"})
    assert r.status_code == 202 and r.json() == {"job_id": 5, "status": "pending"}
    assert depots == [{"sub": "u1", "pid": PID, "data": AUDIO, "filename": "reunion.ogg",
                       "mime": "audio/ogg", "vocabulary": "Voxtral, Otomata",
                       "vocabulary_replace": True}]


def test_sans_vocabulaire_ni_remplacement(client, depots):
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 202
    assert (depots[0]["vocabulary"], depots[0]["vocabulary_replace"]) == (None, False)


def test_sans_jeton_rien_n_est_depose(client, depots):
    r = client.post(CHEMIN, files=_audio())
    assert r.status_code == 401 and depots == []


def test_hors_de_l_org_de_consultation_le_projet_est_introuvable_avant_le_403(
        client, depots, monkeypatch):
    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: False)
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 404 and r.json()["error"] == "unknown_project"
    assert depots == []


def test_un_projet_inconnu(client, depots, monkeypatch):
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: None)
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 404 and r.json()["error"] == "unknown_project"
    assert depots == []


def test_sans_droit_d_ecriture_rien_n_est_lu(client, depots, monkeypatch):
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rtype, rid, want="read": want != "write")
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert depots == []


def test_un_audio_au_dela_du_plafond_est_refuse(client, depots, monkeypatch):
    monkeypatch.setattr(coeur, "MAX_AUDIO_BYTES", len(AUDIO) - 1)
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 413 and r.json()["error"] == "content_too_large"
    assert depots == []


def test_un_formulaire_sans_fichier(client, depots):
    r = client.post(CHEMIN, data={"vocabulary": "x"}, files={"autre": ("a", b"b")},
                    headers=_auth())
    assert r.status_code == 400 and r.json()["error"] == "missing_file"
    assert depots == []


def test_vocabulary_replace_n_accepte_que_true_ou_false(client, depots):
    r = client.post(CHEMIN, files=_audio(), data={"vocabulary_replace": "oui"},
                    headers=_auth())
    assert r.status_code == 400 and r.json()["error"] == "invalid_input"
    assert depots == []


def test_un_refus_du_coeur_est_rendu_nommement(client, monkeypatch):
    def _refus(*a, **k):
        raise AuthzDenied(400, "credential_unavailable", "Aucune clé Mistral posée.")

    monkeypatch.setattr(coeur, "deposer", _refus)
    r = client.post(CHEMIN, files=_audio(), headers=_auth())
    assert r.status_code == 400
    assert r.json() == {"error": "credential_unavailable",
                        "detail": "Aucune clé Mistral posée."}

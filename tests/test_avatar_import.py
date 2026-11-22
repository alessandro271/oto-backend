"""`POST /api/me/avatar/import` : la photo de profil Google, re-hébergée, jamais à la place
d'un upload.

Ce qui est gardé ici :

1. **L'hôte est une liste fermée** : https et `*.googleusercontent.com` seulement — une
   autre adresse, un autre schéma, des identifiants dans l'URL, une adresse qui résout
   vers l'intérieur sont refusés AVANT tout appel réseau ;
2. **un compte qui a déjà un avatar n'est pas touché** (ni téléchargement, ni écriture) ;
3. **une redirection ne sort jamais des hôtes de Google** ;
4. **la lecture est bornée pendant** (la borne d'image du stockage), et ce qui n'est pas
   une image est refusé ;
5. **la pose est conditionnelle** : un upload arrivé entre la lecture et l'écriture
   gagne, et la copie importée est purgée.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import egress, media_store
from oto_mcp.capabilities import media_and_files as mf

_PHOTO = "https://lh3.googleusercontent.com/a/photo=s96-c"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class _Reponse:
    def __init__(self, status=200, corps=_PNG, type_="image/png", location=None):
        self.status_code = status
        self.headers = {"Content-Type": type_}
        if location:
            self.headers["Location"] = location
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.is_permanent_redirect = status in (301, 308)
        self._corps = corps
        self.fermee = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._corps), chunk_size):
            yield self._corps[i:i + chunk_size]

    def close(self):
        self.fermee = True


@pytest.fixture()
def socle(monkeypatch):
    """Un compte SANS avatar, un réseau et un stockage simulés, une résolution publique."""
    vus: dict = {"get": [], "pose": [], "purge": [], "upload": []}
    etat = {"avatar_url": None}
    monkeypatch.setattr(mf.db, "get_user", lambda sub: dict(etat))

    def pose(sub, url, *, seulement_si_absent=False):
        vus["pose"].append((sub, url, seulement_si_absent))
        if seulement_si_absent and etat["avatar_url"]:
            return False
        etat["avatar_url"] = url
        return True
    monkeypatch.setattr(mf.db, "set_avatar_url", pose)

    def upload(prefix, owner, data, ct):
        vus["upload"].append((prefix, owner, len(data)))
        return f"https://cdn.example/{prefix}/{owner}.png"
    monkeypatch.setattr(media_store, "upload_image", upload)
    monkeypatch.setattr(media_store, "delete_by_url", lambda u: vus["purge"].append(u))
    monkeypatch.setattr(egress, "resolved_addresses", lambda hote, port: {"142.250.0.1"})

    reponses: list = []

    def get(url, **kw):
        assert kw.get("allow_redirects") is False, "aucune redirection suivie à l'aveugle"
        vus["get"].append(url)
        return reponses.pop(0) if reponses else _Reponse()
    import requests
    monkeypatch.setattr(requests, "get", get)
    stub_authz(monkeypatch)
    return vus, etat, reponses


def test_importe_la_photo_sur_un_compte_sans_avatar(socle):
    vus, etat, _ = socle
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 200
    assert out == {"ok": True, "imported": True,
                   "avatar_url": "https://cdn.example/avatars/u-1.png"}
    assert vus["get"] == [_PHOTO]
    assert vus["upload"] == [("avatars", "u-1", len(_PNG))]
    assert vus["pose"] == [("u-1", "https://cdn.example/avatars/u-1.png", True)]


def test_un_avatar_existant_n_est_pas_touche(socle):
    vus, etat, _ = socle
    etat["avatar_url"] = "https://cdn.example/avatars/upload.png"
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert (code, out) == (200, {"ok": True, "imported": False,
                                 "avatar_url": "https://cdn.example/avatars/upload.png"})
    assert vus["get"] == [] and vus["upload"] == [] and vus["pose"] == []


@pytest.mark.parametrize("url", [
    "http://lh3.googleusercontent.com/a/photo",          # pas https
    "https://example.com/photo.png",                     # autre hôte
    "https://googleusercontent.com.evil.example/p.png",  # suffixe déguisé
    "https://user:pw@lh3.googleusercontent.com/a/p",     # identifiants dans l'URL
    "file:///etc/passwd",
    "",
])
def test_une_adresse_hors_de_google_est_refusee_sans_appel(socle, url):
    vus, _, _ = socle
    code, out = call("me.avatar.import", body={"url": url})
    assert code == 400 and out["error"] == "url_not_allowed"
    assert vus["get"] == []


def test_un_hote_de_google_qui_resout_vers_l_interieur_est_refuse(socle, monkeypatch):
    vus, _, _ = socle
    monkeypatch.setattr(egress, "resolved_addresses", lambda hote, port: {"127.0.0.1"})
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 400 and out["error"] == "url_not_allowed"
    assert vus["get"] == []


def test_une_redirection_vers_un_autre_hote_est_refusee(socle):
    vus, _, reponses = socle
    reponses.append(_Reponse(302, location="https://example.com/ailleurs.png"))
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 400 and out["error"] == "url_not_allowed"
    assert vus["get"] == [_PHOTO] and vus["upload"] == []


def test_une_redirection_entre_hotes_de_google_est_suivie(socle):
    vus, _, reponses = socle
    autre = "https://lh4.googleusercontent.com/a/photo"
    reponses.extend([_Reponse(302, location=autre), _Reponse()])
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 200 and out["imported"] is True
    assert vus["get"] == [_PHOTO, autre]


def test_ce_qui_n_est_pas_une_image_est_refuse(socle):
    vus, _, reponses = socle
    reponses.append(_Reponse(type_="text/html", corps=b"<html>"))
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 400 and out["error"] == "unsupported_type"
    assert vus["upload"] == []


def test_une_image_trop_lourde_est_coupee_pendant_la_lecture(socle, monkeypatch):
    vus, _, reponses = socle
    monkeypatch.setattr(media_store, "max_image_bytes", lambda: 100)
    reponses.append(_Reponse(corps=b"\0" * 1000))
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 413 and out["error"] == "image_too_large"
    assert vus["upload"] == []


def test_un_echec_de_google_rend_502(socle):
    _, _, reponses = socle
    reponses.append(_Reponse(404))
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert code == 502 and out["error"] == "fetch_failed"


def test_un_upload_arrive_entre_temps_gagne_et_la_copie_est_purgee(socle, monkeypatch):
    """La lecture dit « aucun avatar », l'écriture conditionnelle trouve un upload."""
    vus, etat, _ = socle
    lectures = iter([{"avatar_url": None},
                     {"avatar_url": "https://cdn.example/avatars/upload.png"}])
    monkeypatch.setattr(mf.db, "get_user", lambda sub: next(lectures))
    etat["avatar_url"] = "https://cdn.example/avatars/upload.png"
    code, out = call("me.avatar.import", body={"url": _PHOTO})
    assert (code, out) == (200, {"ok": True, "imported": False,
                                 "avatar_url": "https://cdn.example/avatars/upload.png"})
    assert vus["purge"] == ["https://cdn.example/avatars/u-1.png"]

"""La référence de TRANSIT (`oto_mcp/transit.py`) et sa source `{kind: "transit"}`.

Ce que ce fichier verrouille, sur le VRAI `media_store` (seul le client S3 est faux) :
- le chemin heureux : un dépôt rend une `source` courte, que `file_source.resolve`
  relit côté serveur avec le nom d'origine et le type ;
- l'owner est l'appelant : la référence d'un autre utilisateur ne lit QUE sous la clé
  de l'appelant, et reçoit le refus d'une référence expirée ;
- la forme stricte : traversée, `/` de tête, préfixe hors liste fermée, majuscules,
  saut de ligne final → refus de forme, sans aucune lecture du stockage ;
- une référence bien formée mais absente (purgée) → le refus qui dit l'expiration ;
- le nom d'un fichier n'entre dans une clé que réduit à des caractères sûrs.
"""
from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError

from oto_mcp import file_source as fs
from oto_mcp import media_store, transit


class _S3:
    """Faux client S3 : retient les objets posés et les clés lues."""

    def __init__(self):
        self.objets: dict[str, dict] = {}
        self.lues: list[str] = []

    def put_object(self, *, Bucket, Key, Body, **entetes):
        self.objets[Key] = {"Body": Body, **entetes}

    def head_object(self, *, Bucket, Key):
        self.lues.append(Key)
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}},
                              "HeadObject")
        o = self.objets[Key]
        return {"ContentLength": len(o["Body"]), "ContentType": o.get("ContentType"),
                "Metadata": o.get("Metadata") or {}}

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objets[Key]["Body"])}

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        return f"https://signe.exemple/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.fixture
def s3(monkeypatch):
    faux = _S3()
    monkeypatch.setattr(media_store, "_get_client", lambda: faux)
    monkeypatch.setattr(media_store, "_bucket", lambda: "media-test")
    return faux


def _appelant(monkeypatch, sub):
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: sub)


AUDIO = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x01" * 64
NOM = "[Appel 201]_0600000000_20261021(7).wav"


def _deposer(monkeypatch, sub="sub-a", **kw):
    _appelant(monkeypatch, sub)
    return transit.deposer("threecx-recordings", AUDIO, "audio/x-wav", NOM, **kw)


def test_chemin_heureux_la_source_courte_se_relit_cote_serveur(s3, monkeypatch):
    out = _deposer(monkeypatch)
    src = out["source"]
    assert src["kind"] == "transit" and len(src["ref"]) < 80
    assert "url" not in out and out["size"] == len(AUDIO)
    [cle] = s3.objets
    assert cle.startswith("tmp/threecx-recordings/sub-a/") and "%" not in cle
    rf = fs.resolve(src, max_bytes=10 * 1024 * 1024)
    assert (rf.data, rf.filename, rf.mime) == (AUDIO, NOM, "audio/x-wav")


def test_le_lien_signe_ne_vient_que_sur_demande(s3, monkeypatch):
    out = _deposer(monkeypatch, lien=True)
    [cle] = s3.objets
    assert out["url"].startswith(f"https://signe.exemple/{cle}?")
    assert out["expires_in"] == media_store.presign_expiry()


def test_un_autre_owner_est_refuse_comme_une_reference_expiree(s3, monkeypatch):
    src = _deposer(monkeypatch, sub="sub-a")["source"]
    _appelant(monkeypatch, "sub-b")
    with pytest.raises(fs.FileSourceError, match="expired.*not issued to you"):
        fs.resolve(src)
    assert s3.lues and all(k.startswith("tmp/threecx-recordings/sub-b/") for k in s3.lues)


def test_une_reference_inconnue_dit_qu_elle_a_expire(s3, monkeypatch):
    _appelant(monkeypatch, "sub-a")
    with pytest.raises(fs.FileSourceError,
                       match=r"expired \(transit files are purged after one day\).*re-read"):
        fs.resolve({"kind": "transit", "ref": "aircall-recordings/" + "0" * 32})


@pytest.mark.parametrize("ref", [
    "../avatars/x",
    "/tmp/threecx-recordings/sub-a/" + "0" * 32,
    "tmp/threecx-recordings/" + "0" * 32,
    "threecx-recordings/../../avatars/" + "0" * 32,
    "threecx-recordings/" + "0" * 31 + "/",
    "threecx-recordings/" + "0" * 32 + "/x.wav",
    "threecx-recordings/" + "0" * 32 + "\n",
    "threecx-recordings/" + "A" * 32,
    "avatars/" + "0" * 32,
    "threecx-files/" + "0" * 32,
    "project-files/" + "0" * 32,
    "",
    None,
    42,
])
def test_une_forme_hors_contrat_est_refusee_sans_lire(s3, monkeypatch, ref):
    _appelant(monkeypatch, "sub-a")
    with pytest.raises(fs.FileSourceError, match="must be the reference"):
        fs.resolve({"kind": "transit", "ref": ref})
    assert s3.lues == []


def test_au_dela_du_plafond_refuse_avant_lecture(s3, monkeypatch):
    src = _deposer(monkeypatch)["source"]
    with pytest.raises(fs.FileSourceError, match="larger than 10 bytes"):
        fs.resolve(src, max_bytes=10)


def test_seuls_les_prefixes_de_transit_se_deposent(s3, monkeypatch):
    _appelant(monkeypatch, "sub-a")
    with pytest.raises(transit.TransitError, match="storage error \\(unknown_prefix\\)"):
        transit.deposer("avatars", AUDIO, "audio/x-wav", NOM)
    assert s3.objets == {}


def test_le_kind_inconnu_annonce_transit():
    with pytest.raises(fs.FileSourceError, match="transit"):
        fs.resolve({"kind": "ftp"})


@pytest.mark.parametrize("nom, attendu", [
    ("[Appel 201]_x(7).wav", "Appel_201__x_7_.wav"),
    ("%5BAppel%5D.wav", "5BAppel_5D.wav"),
    ("../../etc/passwd", "etc_passwd"),
    ("/racine.wav", "racine.wav"),
    ("..", "file"),
    ("", "file"),
    (None, "file"),
    ("compte-rendu é.pdf", "compte-rendu_.pdf"),
    ("a" * 300 + ".wav", "a" * 116 + ".wav"),
])
def test_un_nom_n_entre_dans_une_cle_que_reduit_a_des_caracteres_surs(nom, attendu):
    assert media_store.nom_sur(nom) == attendu


def test_upload_private_ne_met_plus_de_pourcent_dans_la_cle(s3):
    media_store.upload_private("threecx-files", "sub-a", b"x", "text/csv", "[a] b.csv")
    [cle] = s3.objets
    assert cle.endswith("/a_b.csv") and "%" not in cle

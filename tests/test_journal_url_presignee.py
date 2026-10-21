"""Une adresse PRÉ-SIGNÉE passée en argument ne s'écrit pas en clair dans le journal.

Mesuré le 15/10/2026 : `tool_calls.args` gardait entière une URL S3 pré-signée passée à
un outil de transcription — `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`
compris, c'est-à-dire l'accès au fichier jusqu'à l'expiration, lisible par les surfaces
de supervision. La règle est celle des journaux serveur (`journal_secrets.masquer_urls`,
une seule fonction), appliquée chaîne par chaîne aux `args`, au SEUL point d'écriture
commun à tous les écrivains (`db.insert_tool_call`).
"""
import json

import pytest

from oto_mcp import calllog
from oto_mcp import journal_secrets as js


def _ecrit(monkeypatch, args, tool="transcription_create"):
    """Les `args` tels que `insert_tool_call` les ÉCRIT (le JSON envoyé à la base),
    après le passage par `truncated_args` comme sur le chemin réel."""
    from oto_mcp.db import usage
    captured = {}

    class _Conn:
        def execute(self, sql, params=None):
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    usage.insert_tool_call({"tool": tool, "ok": True,
                            "args": calllog.truncated_args(args, tool=tool)})
    return json.loads(captured["params"][5])

S3 = ("https://bucket.s3.fr-par.scw.cloud/audio/r.m4a?X-Amz-Algorithm=AWS4-HMAC-SHA256"
      "&X-Amz-Credential=SCWKEYID%2F20261015%2Ffr-par%2Fs3%2Faws4_request"
      "&X-Amz-Date=20261015T080000Z&X-Amz-Expires=3600"
      "&X-Amz-Security-Token=JETONDESESSION&X-Amz-SignedHeaders=host"
      "&X-Amz-Signature=deadbeefSIGNATURE")
SAS = ("https://compte.blob.core.windows.net/c/f.wav?sv=2024-11-04&se=2026-10-16T00%3A00Z"
       "&sr=b&sp=r&sig=SIGAZURE%3D")
GCS = ("https://storage.googleapis.com/b/o?X-Goog-Algorithm=GOOG4-RSA-SHA256"
       "&X-Goog-Credential=svc%40p.iam&X-Goog-Signature=SIGGOOGLE")
SECRETS = ("SCWKEYID", "JETONDESESSION", "deadbeefSIGNATURE", "SIGAZURE", "SIGGOOGLE",
           "svc%40p.iam")


@pytest.mark.parametrize("url", [S3, SAS, GCS])
def test_l_url_presignee_perd_sa_signature_dans_les_args(monkeypatch, url):
    out = _ecrit(monkeypatch, {"url": url, "language": "fr"})
    for secret in SECRETS:
        assert secret not in out["url"]
    assert out["language"] == "fr"
    assert out["url"].startswith(url.split("?")[0] + "?")   # l'adresse reste lisible


def test_ce_qui_n_est_pas_secret_reste_lisible(monkeypatch):
    out = _ecrit(monkeypatch, {"url": S3})["url"]
    for lisible in ("X-Amz-Algorithm=AWS4-HMAC-SHA256", "X-Amz-Date=20261015T080000Z",
                    "X-Amz-Expires=3600", "X-Amz-SignedHeaders=host"):
        assert lisible in out
    out = _ecrit(monkeypatch, {"url": SAS})["url"]
    assert "sv=2024-11-04" in out and "se=2026-10-16T00%3A00Z" in out and "sig=***" in out


def test_une_adresse_imbriquee_et_sous_oto_call_aussi(monkeypatch):
    out = _ecrit(monkeypatch,
                 {"name": "transcription_create", "arguments": {"source": {"url": S3}}},
                 tool="oto_call")
    assert "deadbeefSIGNATURE" not in json.dumps(out)
    out = _ecrit(monkeypatch, {"urls": [S3, GCS]}, tool="x")
    assert "deadbeefSIGNATURE" not in json.dumps(out) and "SIGGOOGLE" not in json.dumps(out)


def test_tout_ecrivain_est_couvert_meme_sans_truncated_args(monkeypatch):
    """Le masque vit au point d'écriture : une ligne posée par un autre écrivain (geste
    REST, worker) avec des `args` STRUCTURÉS (dict, liste) est couverte aussi."""
    from oto_mcp.db import usage
    captured = {}

    class _Conn:
        def execute(self, sql, params=None):
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    usage.insert_tool_call({"tool": "x", "ok": True,
                            "args": {"source": {"urls": [S3], "n": 2}, "ok": True}})
    ecrit = json.loads(captured["params"][5])
    assert "deadbeefSIGNATURE" not in json.dumps(ecrit)
    assert ecrit["source"]["n"] == 2 and ecrit["ok"] is True


def test_l_identite_d_une_adresse_tombe_aussi(monkeypatch):
    out = _ecrit(monkeypatch, {"dsn": "postgres://moi:MOTDEPASSE@hote/base"}, tool="x")
    assert "MOTDEPASSE" not in out["dsn"]


def test_un_texte_sans_adresse_n_est_pas_touche(monkeypatch):
    texte = "what is the key=value? token=abc"
    assert _ecrit(monkeypatch, {"q": texte}, tool="x") == {"q": texte}


@pytest.mark.parametrize("cle", ["X-Amz-Signature", "X-Amz-Credential",
                                 "X-Amz-Security-Token", "X-Goog-Signature",
                                 "X-Goog-Credential", "sig", "signature", "x-api-key",
                                 "Signing-Key"])
def test_les_parametres_de_signature_sont_des_cles_secretes(cle):
    assert js.cle_de_requete_secrete(cle)


@pytest.mark.parametrize("cle", ["sv", "se", "sr", "sp", "X-Amz-Date", "X-Amz-Expires",
                                 "X-Amz-Algorithm", "X-Amz-SignedHeaders", "limit", "q"])
def test_les_parametres_ordinaires_restent_lisibles(cle):
    assert not js.cle_de_requete_secrete(cle)


# --- L'adresse d'une SOURCE de fichier tombe entière, quel que soit l'outil (#1301) ---
#
# La reconnaissance par la forme (clés de signature) ne voit pas un accès porté par le
# chemin ni par une clé de requête au nom ordinaire. Une source `{kind: "url", url}`
# désigne un fichier par le lien qui y donne accès : c'est la propriété qui masque.

PAR_LE_CHEMIN = "https://fichiers.example.com/s/JETONDANSLECHEMIN/rapport.pdf"
CLE_ORDINAIRE = "https://fichiers.example.com/files-pri/a.m4a?t=JETONORDINAIRE"


@pytest.mark.parametrize("tool", ["transcription_create", "pennylane_upload_file"])
@pytest.mark.parametrize("url, secret", [(PAR_LE_CHEMIN, "JETONDANSLECHEMIN"),
                                         (CLE_ORDINAIRE, "JETONORDINAIRE"),
                                         (S3, "deadbeefSIGNATURE")])
def test_l_adresse_d_une_source_tombe_entiere_sur_deux_outils(monkeypatch, tool, url,
                                                              secret):
    out = _ecrit(monkeypatch, {"source": {"kind": "url", "url": url}, "project_id": 4},
                 tool=tool)
    ecrit = json.dumps(out, ensure_ascii=False)
    assert secret not in ecrit and "rapport.pdf" not in ecrit and "a.m4a" not in ecrit
    assert "fichiers.example.com/…#" in ecrit or "scw.cloud/…#" in ecrit  # l'hôte reste
    assert out["project_id"] == 4


def test_la_meme_adresse_donne_le_meme_masque():
    a = js.masquer_urls_des_valeurs({"source": {"kind": "url", "url": PAR_LE_CHEMIN}})
    b = js.masquer_urls_des_valeurs({"x": [{"kind": "url", "url": PAR_LE_CHEMIN}]})
    assert a["source"]["url"] == b["x"][0]["url"]
    assert a["source"]["url"].startswith("https://fichiers.example.com/…#")


def test_une_source_sans_kind_et_une_source_imbriquee_aussi(monkeypatch):
    out = _ecrit(monkeypatch, {"html_source": {"url": PAR_LE_CHEMIN}}, tool="outlook_act")
    assert "JETONDANSLECHEMIN" not in json.dumps(out)
    out = _ecrit(monkeypatch, {"name": "wordpress_media", "arguments": {
        "attachments": [{"kind": "url", "url": CLE_ORDINAIRE}]}}, tool="oto_call")
    assert "JETONORDINAIRE" not in json.dumps(out)


def test_un_ecrivain_sans_truncated_args_masque_la_source(monkeypatch):
    from oto_mcp.db import usage
    captured = {}

    class _Conn:
        def execute(self, sql, params=None):
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    usage.insert_tool_call({"tool": "lemlist_x", "ok": True,
                            "args": {"source": {"kind": "url", "url": PAR_LE_CHEMIN}}})
    assert "JETONDANSLECHEMIN" not in captured["params"][5]


def test_une_adresse_qui_n_est_pas_une_source_reste_lisible(monkeypatch):
    """Seule la SOURCE tombe : l'adresse d'une page à lire (`url` d'un outil web), une
    source désignée autrement (fichier de projet) restent lisibles."""
    out = _ecrit(monkeypatch, {"url": PAR_LE_CHEMIN}, tool="serper_scrape")
    assert out["url"] == PAR_LE_CHEMIN
    pf = {"source": {"kind": "project_file", "project_id": 3, "file_id": 9}}
    assert "'file_id': 9" in _ecrit(monkeypatch, pf, tool="transcription_create")["source"]

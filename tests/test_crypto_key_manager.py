"""Le trousseau du coffre en mode Key Manager (`crypto.py`) : modes déclarés, refus
nommés, `Decrypt` sur une doublure HTTP de Scaleway Key Manager.

La doublure est un VRAI serveur HTTP local : le client (`urllib`) fait la même requête
qu'en production (méthode, chemin, en-tête `X-Auth-Token`, corps JSON) ; seule l'adresse
de base change (`crypto.KMS_API`). Elle « enveloppe » une clé de données comme le ferait
Key Manager : le chiffré rendu par `GenerateDataKey` est opaque, la doublure le reconnaît
et rend le clair en base64.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oto_mcp import crypto

CLE_PLATE = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
DEK = bytes(range(32))
#: Ce que `GenerateDataKey` rendrait (opaque pour le serveur oto).
DEK_ENVELOPPEE = base64.b64encode(b"enveloppe-opaque-de-la-dek").decode()
JETON = "jeton-scw-kms-de-banc-0123456789"
CLE_KMS = "cle-kms-de-banc"
REGION = "fr-par"


class _Doublure:
    """Key Manager en local : `codes` dit ce que rendent les appels successifs."""

    def __init__(self):
        self.appels: list[dict] = []
        self.codes: list[int] = []      # vide = 200
        self.clair = DEK
        doublure = self

        class Gestion(BaseHTTPRequestHandler):
            def do_POST(self):
                corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                doublure.appels.append({"chemin": self.path, "jeton": self.headers.get(
                    "X-Auth-Token"), "type": self.headers.get("Content-Type"), "corps": corps})
                code = doublure.codes.pop(0) if doublure.codes else 200
                if code == 200 and corps.get("ciphertext") != DEK_ENVELOPPEE:
                    code = 400
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if code == 200:
                    self.wfile.write(json.dumps({
                        "key_id": CLE_KMS,
                        "plaintext": base64.b64encode(doublure.clair).decode()}).encode())

            def log_message(self, *args):
                pass

        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), Gestion)
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def fermer(self):
        self.serveur.shutdown()
        self.serveur.server_close()


@pytest.fixture
def kms(monkeypatch, tmp_path):
    """Une doublure de Key Manager, la clé d'API en credential systemd, le cache vidé."""
    d = _Doublure()
    monkeypatch.setattr(crypto, "KMS_API",
                        d.base + "/key-manager/v1alpha1/regions/{region}/keys/{cle}/decrypt")
    monkeypatch.setattr(crypto, "KMS_PAUSE_S", 0)
    monkeypatch.setattr(crypto, "_OUVERTES", None)
    (tmp_path / "scw-kms").write_text(JETON + "\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    for nom in ("OTO_MCP_TROUSSEAU", "OTO_MCP_KMS_CLE", "OTO_MCP_KMS_REGION",
                "OTO_MCP_CLE_ECRITURE"):
        monkeypatch.delenv(nom, raising=False)
    yield d
    d.fermer()


def _mode_kms(monkeypatch, ecriture="01", plate=True):
    if plate:
        monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_PLATE.hex())
    else:
        monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    monkeypatch.setenv("OTO_MCP_TROUSSEAU", json.dumps({"02": DEK_ENVELOPPEE}))
    monkeypatch.setenv("OTO_MCP_KMS_CLE", CLE_KMS)
    monkeypatch.setenv("OTO_MCP_KMS_REGION", REGION)
    monkeypatch.setenv("OTO_MCP_CLE_ECRITURE", ecriture)


# --- mode plate : inchangé -----------------------------------------------------------

def test_mode_plate_inchange(kms, monkeypatch, caplog):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_PLATE.hex())
    assert crypto.trousseau() == {0x01: CLE_PLATE}
    assert crypto.key_ref_d_ecriture() == 0x01
    env = crypto.encrypt("secret", "aad")
    assert base64.b64decode(env)[0] == 0x01
    assert crypto.decrypt(env, "aad") == "secret"
    with caplog.at_level(logging.INFO, logger="oto_mcp.crypto"):
        crypto.charger_trousseau()
    assert "key_ref=0x01 chargée (plate, OTO_MCP_MASTER_KEY), empreinte " \
           + crypto.empreinte_cle(CLE_PLATE)[:12] in caplog.text
    assert "mode plate, écriture sous key_ref=0x01" in caplog.text
    assert kms.appels == []     # Key Manager n'est jamais appelé


def test_ecriture_01_explicite_est_le_defaut(kms, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_PLATE.hex())
    monkeypatch.setenv("OTO_MCP_CLE_ECRITURE", "01")
    assert crypto.key_ref_d_ecriture() == crypto.KEY_REF_ECRITURE == 0x01


# --- mode Key Manager ----------------------------------------------------------------

def test_la_dek_enveloppee_est_rouverte_une_seule_fois(kms, monkeypatch):
    _mode_kms(monkeypatch)
    assert crypto.trousseau() == {0x01: CLE_PLATE, 0x02: DEK}
    assert crypto.trousseau() == {0x01: CLE_PLATE, 0x02: DEK}
    [appel] = kms.appels
    assert appel["chemin"] == f"/key-manager/v1alpha1/regions/{REGION}/keys/{CLE_KMS}/decrypt"
    assert appel["jeton"] == JETON
    assert appel["type"] == "application/json"
    assert appel["corps"] == {"ciphertext": DEK_ENVELOPPEE}


def test_ecriture_02_puis_relecture_02_et_01(kms, monkeypatch):
    _mode_kms(monkeypatch, ecriture="01")
    ancienne = crypto.encrypt("ancien", "aad:1")          # écrite sous 0x01
    assert base64.b64decode(ancienne)[0] == 0x01
    monkeypatch.setenv("OTO_MCP_CLE_ECRITURE", "02")
    neuve = crypto.encrypt("neuf", "aad:2")
    assert base64.b64decode(neuve)[0] == 0x02
    assert crypto.decrypt(neuve, "aad:2") == "neuf"
    assert crypto.decrypt(ancienne, "aad:1") == "ancien"
    # la neuve est bien sous la DEK, pas sous la clé plate
    assert crypto.decrypt_with_key(DEK, neuve, "aad:2") == "neuf"
    assert crypto.cle_d_ecriture() == DEK and crypto.encryption_enabled()


def test_kms_seul_sans_cle_plate(kms, monkeypatch):
    _mode_kms(monkeypatch, ecriture="02", plate=False)
    assert crypto.trousseau() == {0x02: DEK}
    assert crypto.decrypt(crypto.encrypt("x", "aad"), "aad") == "x"


def test_le_boot_journalise_chaque_cle_et_son_empreinte_jamais_la_cle(kms, monkeypatch,
                                                                        caplog):
    _mode_kms(monkeypatch, ecriture="02")
    with caplog.at_level(logging.DEBUG):
        crypto.charger_trousseau()
    texte = caplog.text
    assert "key_ref=0x01 chargée (plate, OTO_MCP_MASTER_KEY), empreinte " \
           + crypto.empreinte_cle(CLE_PLATE)[:12] in texte
    assert (f"key_ref=0x02 chargée (Key Manager, clé {CLE_KMS}, région {REGION}), "
            f"empreinte {crypto.empreinte_cle(DEK)[:12]}") in texte
    assert "mode Key Manager, écriture sous key_ref=0x02" in texte
    for secret in (JETON, DEK.hex(), base64.b64encode(DEK).decode(), CLE_PLATE.hex(),
                   base64.b64encode(CLE_PLATE).decode()):
        assert secret not in texte


# --- refus nommés --------------------------------------------------------------------

@pytest.mark.parametrize("env, motif", [
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_CLE_ECRITURE": "02"},
     r"OTO_MCP_CLE_ECRITURE=02 : la clé 0x02 n'est pas dans le trousseau \(clés déclarées "
     r": 0x01\)"),
    ({"OTO_MCP_CLE_ECRITURE": "02"},
     r"OTO_MCP_CLE_ECRITURE=02 : la clé 0x02 n'est pas dans le trousseau \(clés déclarées "
     r": aucune\)"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": '{"02": "x"}',
      "OTO_MCP_KMS_REGION": REGION},
     r"OTO_MCP_TROUSSEAU porte des clés enveloppées \(0x02\) mais OTO_MCP_KMS_CLE"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": '{"02": "x"}',
      "OTO_MCP_KMS_CLE": CLE_KMS},
     r"mais OTO_MCP_KMS_REGION n'est pas déclarée"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_KMS_CLE": CLE_KMS},
     r"OTO_MCP_KMS_CLE ou OTO_MCP_KMS_REGION déclarée sans OTO_MCP_TROUSSEAU"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": "pas du json"},
     r"OTO_MCP_TROUSSEAU doit être un objet JSON non vide"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": "{}"},
     r"OTO_MCP_TROUSSEAU doit être un objet JSON non vide"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": '{"01": "x"}',
      "OTO_MCP_KMS_CLE": CLE_KMS, "OTO_MCP_KMS_REGION": REGION},
     r"0x01 est la clé plate"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": '{"2": "x"}',
      "OTO_MCP_KMS_CLE": CLE_KMS, "OTO_MCP_KMS_REGION": REGION},
     r"OTO_MCP_TROUSSEAU : '2' n'est pas un key_ref"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_TROUSSEAU": '{"02": ""}',
      "OTO_MCP_KMS_CLE": CLE_KMS, "OTO_MCP_KMS_REGION": REGION},
     r"la clé 0x02 n'a pas de valeur enveloppée"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_CLE_ECRITURE": "zz"},
     r"OTO_MCP_CLE_ECRITURE : 'zz' n'est pas un key_ref"),
    ({"OTO_MCP_MASTER_KEY": CLE_PLATE.hex(), "OTO_MCP_CLE_ECRITURE": "00"},
     r"OTO_MCP_CLE_ECRITURE : '00' n'est pas un key_ref"),
])
def test_une_declaration_incoherente_refuse_de_demarrer(kms, monkeypatch, env, motif):
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    for nom, valeur in env.items():
        monkeypatch.setenv(nom, valeur)
    with pytest.raises(crypto.TrousseauRefuse, match=motif) as e:
        crypto.charger_trousseau()
    assert "démarrage refusé" in str(e.value)
    assert kms.appels == []


def test_aucune_cle_refuse_de_demarrer(kms, monkeypatch):
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    with pytest.raises(crypto.TrousseauRefuse, match=r"aucune clé de coffre déclarée"):
        crypto.charger_trousseau()
    # hors démarrage, le trousseau vide reste vide (encrypt/decrypt lèvent au 1er usage)
    assert crypto.trousseau() == {}


def test_sans_credential_scw_kms_refus_nomme(kms, monkeypatch, tmp_path):
    _mode_kms(monkeypatch)
    (tmp_path / "scw-kms").unlink()
    with pytest.raises(crypto.TrousseauRefuse, match=r"LoadCredential=scw-kms:/etc/oto-mcp/scw-kms.key"):
        crypto.charger_trousseau()
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    with pytest.raises(crypto.TrousseauRefuse, match=r"aucune clé d'API"):
        crypto.charger_trousseau()
    assert kms.appels == []


def test_un_refus_de_key_manager_ne_se_rejoue_pas(kms, monkeypatch):
    _mode_kms(monkeypatch)
    kms.codes = [403]
    with pytest.raises(crypto.TrousseauRefuse,
                       match=rf"Key Manager refuse le déchiffrement : clé {CLE_KMS}, "
                             rf"région {REGION}, HTTP 403 — démarrage refusé") as e:
        crypto.charger_trousseau()
    assert not isinstance(e.value, crypto.KeyManagerInjoignable)
    assert len(kms.appels) == 1
    assert JETON not in str(e.value) and e.value.__cause__ is None


def test_une_dek_qui_n_a_pas_32_octets_refuse(kms, monkeypatch):
    _mode_kms(monkeypatch)
    kms.clair = b"\x01" * 16
    with pytest.raises(crypto.TrousseauRefuse, match=r"0x02 .* ne fait pas 32 octets"):
        crypto.charger_trousseau()


def test_une_erreur_passagere_est_rejouee(kms, monkeypatch):
    _mode_kms(monkeypatch)
    kms.codes = [503, 429]
    assert crypto.trousseau()[0x02] == DEK
    assert len(kms.appels) == 3


# --- Key Manager injoignable ---------------------------------------------------------

def test_injoignable_trois_essais_puis_refus(kms, monkeypatch, caplog):
    _mode_kms(monkeypatch)
    kms.codes = [503, 502, 500]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(crypto.KeyManagerInjoignable,
                           match=rf"^Key Manager injoignable : clé {CLE_KMS}, région "
                                 rf"{REGION} — démarrage refusé") as e:
            crypto.charger_trousseau()
    assert len(kms.appels) == 3 == crypto.KMS_ESSAIS
    assert caplog.text.count("Key Manager : essai") == 3
    assert JETON not in caplog.text and JETON not in str(e.value)


def test_injoignable_par_le_reseau_et_le_delai(kms, monkeypatch):
    """Un port fermé (connexion refusée), puis un serveur qui ne répond pas dans le
    délai : 3 essais chacun, puis le refus nommé."""
    _mode_kms(monkeypatch)
    kms.fermer()
    monkeypatch.setattr(crypto, "KMS_API", kms.base + "/regions/{region}/keys/{cle}/decrypt")
    with pytest.raises(crypto.KeyManagerInjoignable, match=r"3 essais"):
        crypto.trousseau()

    import socket
    muet = socket.socket()
    muet.bind(("127.0.0.1", 0))
    muet.listen(8)                  # accepte la connexion, ne répond jamais
    try:
        monkeypatch.setattr(crypto, "KMS_DELAI_S", 0.2)
        monkeypatch.setattr(
            crypto, "KMS_API",
            f"http://127.0.0.1:{muet.getsockname()[1]}/regions/{{region}}/keys/{{cle}}/decrypt")
        with pytest.raises(crypto.KeyManagerInjoignable,
                           match=r"3 essais de 0.2 s, dernier : (TimeoutError|timeout|URLError)"):
            crypto.trousseau()
    finally:
        muet.close()


def test_une_cle_enveloppee_illisible_ne_tombe_pas_en_mode_plate(kms, monkeypatch):
    """Jamais de repli : Key Manager en panne → le trousseau lève, il ne rend pas la
    seule clé plate."""
    _mode_kms(monkeypatch)
    kms.codes = [500, 500, 500]
    with pytest.raises(crypto.KeyManagerInjoignable):
        crypto.encrypt("x", "aad")

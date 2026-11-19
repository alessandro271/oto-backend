"""Chiffrement par enveloppe des secrets au repos.

AES-256-GCM. Enveloppe (base64) = key_ref(1o) ‖ nonce(12o) ‖ ciphertext+tag. L'AAD =
identité de la ligne (table:entity_type:entity_id:connector) lie le ciphertext à SA
ligne : un blob ne peut pas être transplanté vers un autre connecteur/entité. `key_ref`
ne fait PAS partie de l'AAD : changer la clé d'une ligne ne change pas son identité.

**Le trousseau** (`trousseau`) : `{key_ref: clé}`, les clés que CETTE instance sait
ouvrir. `decrypt` lit `key_ref` (premier octet de l'enveloppe) et y prend sa clé
(`cle_du_trousseau`) ; `encrypt` écrit sous le `key_ref` d'écriture déclaré
(`key_ref_d_ecriture`, `OTO_MCP_CLE_ECRITURE`, `01` par défaut). `trousseau` est le SEUL
point qui sait où vivent les clés. Un `key_ref` absent du trousseau lève `CleInconnue`,
distincte d'une enveloppe qui ne s'ouvre pas (`EnveloppeIndechiffrable`, `InvalidTag`).

**Les modes, DÉCLARÉS, jamais devinés** (`declaration`, docs/connector-vault.md) :
- **plate** : `OTO_MCP_MASTER_KEY` seule (32 octets, base64 ou hex) = `key_ref 0x01`,
  sous laquelle toute enveloppe existante est écrite ;
- **Key Manager** : `OTO_MCP_TROUSSEAU` (`{"02": "<clé de données enveloppée>"}`, la
  sortie de `GenerateDataKey` de Scaleway Key Manager), `OTO_MCP_KMS_CLE` (l'id de la
  clé Key Manager qui l'enveloppe) et `OTO_MCP_KMS_REGION`. Chaque clé enveloppée est
  ouverte UNE fois par processus (`Decrypt`, `dechiffrer_par_key_manager`) et ne vit
  qu'en RAM : jamais sur disque, jamais dans l'environnement. La clé d'API qui appelle
  `Decrypt` arrive par `LoadCredential=scw-kms` (`$CREDENTIALS_DIRECTORY/scw-kms`).
  Pendant la transition, `OTO_MCP_MASTER_KEY` reste là pour lire `0x01`.
Une déclaration incohérente lève `TrousseauRefuse`, et le serveur ne démarre pas
(`charger_trousseau`, appelé par `server.main`). Les autres processus (maintenance,
scripts du lanceur, export) passent par le même `trousseau`, ouvert à leur premier usage.

Chiffrement **obligatoire** : il n'existe plus aucune colonne plaintext (purge
2026-06-11). `encryption_enabled()` reste exposé, mais `encrypt`/`decrypt`
LÈVENT si aucune clé n'est chargée → pas de stockage en clair silencieux.

« Dump Postgres = ciphertext only » : vrai. La liste des colonnes chiffrées est
`export_perimetre/rechiffrement.COLONNES_CHIFFREES`, gardée contre le schéma et contre
les appels à `encrypt`.

Une clé erronée → InvalidTag (échec bruyant), pas de mauvais déchiffrement silencieux.
Perte de la master key = perte totale des secrets chiffrés → la sauvegarder hors-DB
(Secret Manager versionné + escrow), sur un cycle de backup distinct de la DB.

`encrypt_with_key`/`decrypt_with_key`/`seal`/`unseal` prennent une clé DONNÉE, hors
trousseau : le rechiffrement d'un export vers la clé d'une AUTRE instance.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

#: Le `key_ref` de la clé plate (`OTO_MCP_MASTER_KEY`) : toute enveloppe existante le porte.
CLE_PLATE = 0x01

#: Le `key_ref` d'écriture par défaut, quand l'instance ne déclare pas
#: `OTO_MCP_CLE_ECRITURE` (`key_ref_d_ecriture`).
KEY_REF_ECRITURE = CLE_PLATE

# --- Scaleway Key Manager, `Decrypt` -------------------------------------------------
# Forme vérifiée dans le SDK officiel, dépôt public `scaleway/scaleway-sdk-python`,
# `scaleway/scaleway/key_manager/v1alpha1/api.py` (`KeyManagerV1Alpha1API.decrypt` :
# `POST /key-manager/v1alpha1/regions/{region}/keys/{key_id}/decrypt`) et
# `marshalling.py` (`marshal_DecryptRequest` : corps `{"ciphertext": …}` ;
# `unmarshal_DecryptResponse` : `{"key_id", "plaintext", "ciphertext"?}`). Le jeton passe
# par `X-Auth-Token`, comme pour le Secret Manager (`deploy/lanceur_secrets.py`). Le
# `ciphertext` est la chaîne que rend `GenerateDataKey`, telle quelle ; le `plaintext`
# rendu est en base64 (dépôt `scaleway/docs-content`,
# `pages/key-manager/api-cli/encrypt-decrypt-data-with-km-dek.mdx` : `… | base64 -d`).
KMS_API = ("https://api.scaleway.com/key-manager/v1alpha1/regions/{region}"
           "/keys/{cle}/decrypt")
#: Délai d'un essai, comme `DELAI_S` du lanceur ; puis `KMS_ESSAIS` essais au plus.
KMS_DELAI_S = 15
KMS_ESSAIS = 3
KMS_PAUSE_S = 2.0
#: Le nom du credential systemd qui porte la clé d'API de `Decrypt`
#: (`LoadCredential=scw-kms`, fichier `/etc/credstore/scw-kms`).
CREDENTIAL_KMS = "scw-kms"


class EnveloppeIndechiffrable(RuntimeError):
    """Une enveloppe qui ne se déchiffre pas sous cette AAD et cette clé (clé maître
    périmée, enveloppe corrompue, ou chiffrée pour une AUTRE ligne). `RuntimeError` par
    héritage — les appelants qui l'attrapaient ainsi n'ont rien à changer —, typée pour
    qu'un appelant qui sait que son état perdu n'est qu'un cache l'attrape SEUL, sans
    avaler une master key absente."""


class CleInconnue(RuntimeError):
    """Une enveloppe dont le `key_ref` n'est pas dans le trousseau de cette instance : la
    clé qui l'ouvre n'est pas chargée. Ce n'est PAS une enveloppe périmée
    (`EnveloppeIndechiffrable`) : un appelant qui jette un cache illisible ne doit pas
    l'avaler, la valeur se relira quand la clé sera là."""


class TrousseauRefuse(RuntimeError):
    """La déclaration des clés de cette instance est incohérente, ou une clé déclarée
    ne s'ouvre pas : le serveur refuse de démarrer, le message nomme pourquoi. Il ne
    porte jamais une clé, ni le jeton de Key Manager."""


class KeyManagerInjoignable(TrousseauRefuse):
    """Key Manager n'a pas répondu après `KMS_ESSAIS` essais."""


@dataclass(frozen=True)
class Declaration:
    """Ce que l'instance déclare de ses clés, lu dans l'environnement et validé."""
    plate: Optional[bytes]            # OTO_MCP_MASTER_KEY, key_ref 0x01
    enveloppees: tuple[tuple[int, str], ...]   # OTO_MCP_TROUSSEAU, (key_ref, chiffré)
    kms_cle: str                      # OTO_MCP_KMS_CLE
    kms_region: str                   # OTO_MCP_KMS_REGION
    ecriture: int                     # OTO_MCP_CLE_ECRITURE

    @property
    def refs(self) -> set[int]:
        return ({CLE_PLATE} if self.plate is not None else set()) | {
            k for k, _ in self.enveloppees}


def _load_master_key() -> Optional[bytes]:
    """La clé plate depuis l'env (32 octets), None si absente — à lire par le
    trousseau (`trousseau`), jamais directement.

    Accepte hex (64 chars) OU base64 — détection EXPLICITE du format (sinon une
    clé hex décode aussi en base64 → 48 octets → refus de boot).
    """
    raw = os.environ.get("OTO_MCP_MASTER_KEY")
    if not raw:
        return None
    return parse_key(raw, "OTO_MCP_MASTER_KEY")


def _nom_ref(key_ref: int) -> str:
    return f"0x{key_ref:02x}"


def _lire_key_ref(texte: str, nom: str) -> int:
    """Un `key_ref` écrit en deux chiffres hexadécimaux (`01`, `02`…), de 0x01 à 0xff."""
    t = texte.strip()
    if len(t) != 2 or not all(c in string.hexdigits for c in t) or int(t, 16) == 0:
        raise TrousseauRefuse(f"{nom} : {t!r} n'est pas un key_ref (deux chiffres "
                              "hexadécimaux, de 01 à ff) — démarrage refusé")
    return int(t, 16)


def _lire_enveloppees(brut: str) -> tuple[tuple[int, str], ...]:
    """`OTO_MCP_TROUSSEAU` : `{"02": "<clé de données enveloppée>"}`. Les messages ne
    citent jamais une valeur."""
    try:
        objet = json.loads(brut)
    except ValueError:
        objet = None
    if not isinstance(objet, dict) or not objet:
        raise TrousseauRefuse('OTO_MCP_TROUSSEAU doit être un objet JSON non vide '
                              '{"02": "<clé de données enveloppée>"} — démarrage refusé')
    enveloppees = {}
    for cle, chiffre in objet.items():
        key_ref = _lire_key_ref(cle, "OTO_MCP_TROUSSEAU")
        if key_ref == CLE_PLATE:
            raise TrousseauRefuse("OTO_MCP_TROUSSEAU : 0x01 est la clé plate "
                                  "(OTO_MCP_MASTER_KEY), jamais une clé enveloppée — "
                                  "démarrage refusé")
        if key_ref in enveloppees:
            raise TrousseauRefuse(f"OTO_MCP_TROUSSEAU nomme deux fois {_nom_ref(key_ref)} "
                                  "— démarrage refusé")
        if not isinstance(chiffre, str) or not chiffre.strip():
            raise TrousseauRefuse(f"OTO_MCP_TROUSSEAU : la clé {_nom_ref(key_ref)} n'a pas "
                                  "de valeur enveloppée — démarrage refusé")
        enveloppees[key_ref] = chiffre.strip()
    return tuple(sorted(enveloppees.items()))


def declaration() -> Declaration:
    """Les clés que l'instance DÉCLARE, validées. Lève `TrousseauRefuse` sur toute
    incohérence ; une instance qui ne déclare rien rend une déclaration vide (le refus
    de démarrer sans clé est celui de `charger_trousseau`)."""
    plate = _load_master_key()
    brut = os.environ.get("OTO_MCP_TROUSSEAU", "").strip()
    kms_cle = os.environ.get("OTO_MCP_KMS_CLE", "").strip()
    kms_region = os.environ.get("OTO_MCP_KMS_REGION", "").strip()
    ecriture = _lire_key_ref(os.environ.get("OTO_MCP_CLE_ECRITURE", "01"),
                             "OTO_MCP_CLE_ECRITURE")
    enveloppees = _lire_enveloppees(brut) if brut else ()
    if enveloppees:
        manque = [n for n, v in (("OTO_MCP_KMS_CLE", kms_cle),
                                 ("OTO_MCP_KMS_REGION", kms_region)) if not v]
        if manque:
            raise TrousseauRefuse(
                "OTO_MCP_TROUSSEAU porte des clés enveloppées ("
                + ", ".join(_nom_ref(k) for k, _ in enveloppees) + ") mais "
                + " et ".join(manque) + " n'est pas déclarée : rien ne dit quelle clé "
                "Key Manager les ouvre — démarrage refusé")
    elif kms_cle or kms_region:
        raise TrousseauRefuse(
            "OTO_MCP_KMS_CLE ou OTO_MCP_KMS_REGION déclarée sans OTO_MCP_TROUSSEAU : "
            "aucune clé enveloppée à ouvrir — déclarer le trousseau, ou retirer ces "
            "variables — démarrage refusé")
    d = Declaration(plate, enveloppees, kms_cle, kms_region, ecriture)
    if ecriture not in d.refs and (d.refs or ecriture != KEY_REF_ECRITURE):
        chargees = ", ".join(_nom_ref(k) for k in sorted(d.refs)) or "aucune"
        raise TrousseauRefuse(
            f"OTO_MCP_CLE_ECRITURE={ecriture:02x} : la clé {_nom_ref(ecriture)} n'est pas "
            f"dans le trousseau (clés déclarées : {chargees}) — démarrage refusé")
    return d


# --- les clés enveloppées, ouvertes une fois par processus -------------------------

_VERROU = threading.Lock()
#: (signature de la déclaration Key Manager, clés ouvertes). En RAM seulement.
_OUVERTES: Optional[tuple[tuple, dict[int, bytes]]] = None
_ouvrir = urllib.request.urlopen


def _jeton_key_manager() -> str:
    """La clé d'API de `Decrypt`, depuis `$CREDENTIALS_DIRECTORY/scw-kms` (systemd,
    `LoadCredential=scw-kms`) — jamais de l'environnement."""
    dossier = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not dossier:
        raise TrousseauRefuse(
            "Key Manager : aucune clé d'API — l'unité doit passer "
            f"LoadCredential={CREDENTIAL_KMS} (fichier /etc/credstore/{CREDENTIAL_KMS}) — "
            "démarrage refusé")
    try:
        jeton = (Path(dossier) / CREDENTIAL_KMS).read_text(encoding="utf-8").strip()
    except OSError as erreur:
        raise TrousseauRefuse(
            f"Key Manager : clé d'API illisible ({type(erreur).__name__}) — l'unité doit "
            f"passer LoadCredential={CREDENTIAL_KMS} (fichier "
            f"/etc/credstore/{CREDENTIAL_KMS}) — démarrage refusé") from None
    if not jeton:
        raise TrousseauRefuse("Key Manager : clé d'API vide — démarrage refusé")
    return jeton


def dechiffrer_par_key_manager(chiffre: str, cle: str, region: str, jeton: str) -> bytes:
    """`Decrypt` de Scaleway Key Manager : la clé de données en clair, en octets.

    `KMS_ESSAIS` essais de `KMS_DELAI_S` s sur une panne de réseau, un 429 ou un 5xx,
    puis `KeyManagerInjoignable`. Un autre refus (4xx : droits, clé inconnue, chiffré
    refusé) ne se rejoue pas. Aucun message ne porte le jeton."""
    url = KMS_API.format(region=urllib.parse.quote(region, safe=""),
                         cle=urllib.parse.quote(cle, safe=""))
    corps = json.dumps({"ciphertext": chiffre}).encode("utf-8")
    dernier = ""
    for essai in range(1, KMS_ESSAIS + 1):
        requete = urllib.request.Request(
            url, data=corps, method="POST",
            headers={"X-Auth-Token": jeton, "Content-Type": "application/json"})
        try:
            with _ouvrir(requete, timeout=KMS_DELAI_S) as reponse:
                brut = reponse.read()
        except urllib.error.HTTPError as erreur:
            if erreur.code != 429 and erreur.code < 500:
                raise TrousseauRefuse(
                    f"Key Manager refuse le déchiffrement : clé {cle}, région {region}, "
                    f"HTTP {erreur.code} — démarrage refusé") from None
            dernier = f"HTTP {erreur.code}"
        except OSError as erreur:   # URLError, délai dépassé, connexion refusée
            dernier = type(erreur).__name__
        else:
            return _clair_de_la_reponse(brut, cle, region)
        logger.warning("Key Manager : essai %d/%d en échec (clé %s, région %s : %s)",
                       essai, KMS_ESSAIS, cle, region, dernier)
        if essai < KMS_ESSAIS:
            time.sleep(KMS_PAUSE_S)
    raise KeyManagerInjoignable(
        f"Key Manager injoignable : clé {cle}, région {region} — démarrage refusé "
        f"({KMS_ESSAIS} essais de {KMS_DELAI_S} s, dernier : {dernier})")


def _clair_de_la_reponse(brut: bytes, cle: str, region: str) -> bytes:
    try:
        clair = base64.b64decode(json.loads(brut.decode("utf-8"))["plaintext"],
                                 validate=True)
    except (ValueError, KeyError, TypeError):
        raise TrousseauRefuse(f"Key Manager : réponse illisible pour la clé {cle}, "
                              f"région {region} — démarrage refusé") from None
    return clair


def _cles_enveloppees(d: Declaration) -> dict[int, bytes]:
    """Les clés enveloppées de `d`, ouvertes par Key Manager UNE fois par processus (et
    de nouveau seulement si la déclaration change). Elles ne quittent pas la RAM."""
    global _OUVERTES
    signature = (d.kms_cle, d.kms_region, d.enveloppees)
    with _VERROU:
        if _OUVERTES is not None and _OUVERTES[0] == signature:
            return _OUVERTES[1]
        jeton = _jeton_key_manager()
        cles = {}
        for key_ref, chiffre in d.enveloppees:
            cle = dechiffrer_par_key_manager(chiffre, d.kms_cle, d.kms_region, jeton)
            if len(cle) != 32:
                raise TrousseauRefuse(
                    f"OTO_MCP_TROUSSEAU : la clé {_nom_ref(key_ref)} ouverte par Key "
                    f"Manager ne fait pas 32 octets (AES-256) — démarrage refusé")
            cles[key_ref] = cle
        _OUVERTES = (signature, cles)
        return cles


def trousseau() -> dict[int, bytes]:
    """`{key_ref: clé}` : les clés que cette instance sait ouvrir, vide sans clé. La
    déclaration est relue à chaque appel ; une clé enveloppée n'est ouverte qu'une fois."""
    d = declaration()
    cles = {} if d.plate is None else {CLE_PLATE: d.plate}
    if d.enveloppees:
        cles.update(_cles_enveloppees(d))
    return cles


def cle_du_trousseau(key_ref: int, cles: Optional[dict[int, bytes]] = None) -> bytes:
    """La clé de `key_ref` dans le trousseau (`cles`, déjà lu, ou relu). Lève
    `RuntimeError` si le trousseau est vide, `CleInconnue` si ce `key_ref` n'y est pas."""
    if cles is None:
        cles = trousseau()
    if not cles:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — déchiffrement impossible")
    try:
        return cles[key_ref]
    except KeyError:
        raise CleInconnue(
            f"enveloppe sous la clé key_ref=0x{key_ref:02x}, absente du trousseau de "
            f"cette instance (clés chargées : "
            f"{', '.join(f'0x{k:02x}' for k in sorted(cles))})") from None


def key_ref_d_ecriture() -> int:
    """Le `key_ref` sous lequel `encrypt` écrit : `OTO_MCP_CLE_ECRITURE`, `01` par défaut."""
    return declaration().ecriture


def cle_d_ecriture() -> Optional[bytes]:
    """La clé sous laquelle `encrypt` écrit (`key_ref_d_ecriture`), None sans clé."""
    return trousseau().get(key_ref_d_ecriture())


def empreinte_cle(cle: bytes) -> str:
    """Ce qui désigne une clé sans la révéler : un haché à séparation de domaine (la clé
    est 256 bits d'aléa : rien à deviner à partir de son haché). Le manifeste d'un export
    et le journal de démarrage la citent."""
    return hashlib.sha256(b"oto-export-perimetre/cle:" + cle).hexdigest()


def charger_trousseau() -> None:
    """Au démarrage du serveur (`server.main`), avant tout le reste : valide la
    déclaration, ouvre les clés enveloppées, et journalise pour chaque clé son
    `key_ref`, son origine et une empreinte courte — jamais la clé. Une instance sans
    aucune clé, ou une déclaration incohérente, ou Key Manager injoignable : refus de
    démarrer (`TrousseauRefuse`)."""
    d = declaration()
    cles = trousseau()
    if not cles:
        raise TrousseauRefuse(
            "aucune clé de coffre déclarée : ni OTO_MCP_MASTER_KEY (mode plate) ni "
            "OTO_MCP_TROUSSEAU (mode Key Manager) — démarrage refusé")
    for key_ref, cle in sorted(cles.items()):
        origine = ("plate, OTO_MCP_MASTER_KEY" if key_ref == CLE_PLATE else
                   f"Key Manager, clé {d.kms_cle}, région {d.kms_region}")
        logger.info("coffre : clé key_ref=%s chargée (%s), empreinte %s",
                    _nom_ref(key_ref), origine, empreinte_cle(cle)[:12])
    logger.info("coffre : mode %s, écriture sous key_ref=%s",
                "Key Manager" if d.enveloppees else "plate", _nom_ref(d.ecriture))


def parse_key(raw: str, nom: str) -> bytes:
    """Une clé maîtresse écrite en hex (64 chars) ou base64, décodée en 32 octets —
    `nom` dit d'où elle vient dans le refus."""
    raw = raw.strip()
    if len(raw) == 64 and all(c in string.hexdigits for c in raw):
        key = bytes.fromhex(raw)
    else:
        key = base64.b64decode(raw, validate=True)
    if len(key) != 32:
        raise ValueError(f"{nom} doit décoder en 32 octets (AES-256 ; hex 64 chars ou base64)")
    return key


def encryption_enabled() -> bool:
    return cle_d_ecriture() is not None


def encrypt(plaintext: str, aad: str) -> str:
    key_ref = key_ref_d_ecriture()
    key = trousseau().get(key_ref)
    if key is None:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — chiffrement indisponible")
    return base64.b64encode(seal(key, plaintext.encode(), aad, key_ref)).decode()


def decrypt(envelope: str, aad: str) -> str:
    cles = trousseau()
    if not cles:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — déchiffrement impossible")
    blob = base64.b64decode(envelope)
    if not blob:
        raise ValueError("enveloppe vide")
    return unseal(cle_du_trousseau(blob[0], cles), blob, aad).decode()


def encrypt_with_key(key: bytes, plaintext: str, aad: str) -> str:
    """L'enveloppe, sous une clé DONNÉE plutôt que celle de l'instance : le
    rechiffrement d'un export vers une autre instance (#1088) tient deux clés à la
    fois, en mémoire. Même format que `encrypt` ; `key_ref` = `CLE_PLATE`, la clé
    plate de la cible : seule une clé plate existe en clair hors de son instance (une
    clé de données Key Manager ne s'ouvre que chez elle), et l'import vérifie la clé
    0x01 de la cible (`export_perimetre.importation.cle_de_l_instance`)."""
    return base64.b64encode(seal(key, plaintext.encode(), aad)).decode()


def decrypt_with_key(key: bytes, envelope: str, aad: str) -> str:
    return unseal(key, base64.b64decode(envelope), aad).decode()


def seal(key: bytes, data: bytes, aad: str, key_ref: int = CLE_PLATE) -> bytes:
    """L'enveloppe BRUTE (key_ref ‖ nonce ‖ ciphertext+tag), pour des octets : un objet
    d'archive (#1088) n'a pas à passer par du texte base64 pour être chiffré. `key_ref`
    dit sous quelle clé du trousseau `key` est rangée."""
    nonce = os.urandom(12)
    return bytes([key_ref]) + nonce + AESGCM(key).encrypt(nonce, data, aad.encode())


def unseal(key: bytes, blob: bytes, aad: str) -> bytes:
    """Ouvre `blob` sous `key`, DONNÉE : le choix de la clé selon `blob[0]` (key_ref)
    est fait avant, par `decrypt` ; [1:13] = nonce."""
    nonce, ct = blob[1:13], blob[13:]
    try:
        return AESGCM(key).decrypt(nonce, ct, aad.encode())
    except InvalidTag as e:
        # InvalidTag a un str() VIDE : sans cette traduction, l'erreur remonte
        # illisible jusqu'à l'agent (« Error calling tool X: » sans message) et
        # jusqu'à Sentry (issue « InvalidTag » nue). Cause = enveloppe chiffrée
        # avec une master key ≠ courante (clé périmée) ou AAD ≠ identité de ligne.
        # Message actionnable : reconnecter le connecteur le re-chiffre avec la clé courante.
        raise EnveloppeIndechiffrable(
            "credential indéchiffrable (chiffré avec une clé périmée ou enveloppe "
            "corrompue) — reconnecte ce connecteur depuis le dashboard"
        ) from e

"""Chiffrement par enveloppe des secrets au repos.

AES-256-GCM. Enveloppe (base64) = key_ref(1o) ‖ nonce(12o) ‖ ciphertext+tag. L'AAD =
identité de la ligne (table:entity_type:entity_id:connector) lie le ciphertext à SA
ligne : un blob ne peut pas être transplanté vers un autre connecteur/entité. `key_ref`
ne fait PAS partie de l'AAD : changer la clé d'une ligne ne change pas son identité.

**Le trousseau** (`trousseau`) : `{key_ref: clé}`, les clés que CETTE instance sait
ouvrir. `decrypt` lit `key_ref` (premier octet de l'enveloppe) et y prend sa clé
(`cle_du_trousseau`) ; `encrypt` écrit sous le `key_ref` d'écriture (`KEY_REF_ECRITURE`).
Aujourd'hui le trousseau ne porte qu'une clé, `CLE_PLATE` (0x01) = la clé hors DB de
l'env `OTO_MCP_MASTER_KEY` (32 octets, base64 ou hex), sous laquelle toute enveloppe
existante est écrite. `trousseau`/`cle_du_trousseau` sont le SEUL point qui sait où
vivent les clés : y ajouter un `key_ref` (une clé déballée au démarrage, une clé par
ligne) ne touche aucun appelant. Un `key_ref` absent du trousseau lève `CleInconnue`,
distincte d'une enveloppe qui ne s'ouvre pas (`EnveloppeIndechiffrable`, `InvalidTag`).

Chiffrement **obligatoire** : il n'existe plus aucune colonne plaintext (purge
2026-06-11). `encryption_enabled()` reste exposé, mais `encrypt`/`decrypt`
LÈVENT si la master key est absente → un serveur sans `OTO_MCP_MASTER_KEY` boote
mais tout write de credential échoue fort (pas de stockage en clair silencieux).

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
import os
import string
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Le `key_ref` de la clé plate (`OTO_MCP_MASTER_KEY`) : toute enveloppe existante le porte.
CLE_PLATE = 0x01

#: Le `key_ref` sous lequel `encrypt` écrit. Une constante tant que l'instance n'en
#: déclare pas d'autre.
KEY_REF_ECRITURE = CLE_PLATE


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


def trousseau() -> dict[int, bytes]:
    """`{key_ref: clé}` : les clés que cette instance sait ouvrir, vide sans clé.
    Relu à chaque appel, comme la clé l'était."""
    cle = _load_master_key()
    return {} if cle is None else {CLE_PLATE: cle}


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


def cle_d_ecriture() -> Optional[bytes]:
    """La clé sous laquelle `encrypt` écrit (`KEY_REF_ECRITURE`), None sans clé."""
    return trousseau().get(KEY_REF_ECRITURE)


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
    key = cle_d_ecriture()
    if key is None:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — chiffrement indisponible")
    return base64.b64encode(seal(key, plaintext.encode(), aad, KEY_REF_ECRITURE)).decode()


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
    plate de la cible."""
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

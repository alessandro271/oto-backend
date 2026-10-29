"""Référence de TRANSIT — désigner un fichier déposé par un outil sans recopier d'URL.

Un outil qui sert un fichier qu'un autre outil consommera (l'audio d'un enregistrement
d'appel, que la transcription lit) le dépose en transit (`deposer`) et rend une
`source` courte : `{"kind": "transit", "ref": "<prefix>/<empreinte>"}`. Le consommateur
la passe à `file_source.resolve`, qui relit les octets côté serveur (`lire`).

Pourquoi : une URL présignée fait 600 à 1 000 caractères (jeton de session compris), et
un agent qui la recopie dans un autre appel l'altère parfois — refus 403/400 de
l'hébergeur, sans rien qui dise pourquoi. Une référence d'une cinquantaine de caractères,
un préfixe puis de l'hexadécimal, se recopie.

Garde :
- **l'owner est l'appelant, par construction** : la référence ne le porte pas, la clé
  se recompose avec le `sub` de l'appel, le même qu'au dépôt (`cle_transit`). Une
  référence émise pour un autre utilisateur désigne une clé absente : même refus
  qu'une référence purgée — distinguer les deux dirait qu'un fichier existe ;
- **forme stricte** : un préfixe de la liste FERMÉE `media_store.PREFIXES_TRANSIT`, puis
  32 caractères hexadécimaux. Aucun `/` de tête, `..` ni préfixe arbitraire ne passe :
  rien hors de `tmp/<préfixe de transit>/<appelant>/` ;
- **éphémère** : la règle de cycle de vie du stockage purge `tmp/` en un jour ; un
  consommateur qui n'en a plus besoin le supprime avant (`supprimer`).

Appels BLOQUANTS (I/O S3) : depuis un handler sync (threadpool), jamais dans la boucle.
"""
from __future__ import annotations

import re
from typing import Optional

from . import access, media_store

#: `<préfixe>/<empreinte>` — le préfixe est vérifié ensuite contre la liste fermée.
_FORME = re.compile(r"([a-z0-9-]{1,40})/([0-9a-f]{32})")


class TransitError(RuntimeError):
    """Dépôt impossible, ou référence mal formée, expirée ou étrangère à l'appelant."""


def deposer(prefix: str, data: bytes, mime: str, filename: str, *,
            lien: bool = False) -> dict:
    """Dépose `data` en transit sous l'appelant et rend ce que l'outil sert : la
    `source` à passer telle quelle, le nom, le type, la taille — et, si `lien`, une URL
    signée (pour l'écouter ou la télécharger, jamais pour la recopier dans un appel)."""
    sub = access.current_user_sub_or_raise()
    try:
        digest = media_store.upload_transit(prefix, sub, data, mime, filename)
        out = {"source": {"kind": "transit", "ref": f"{prefix}/{digest}"},
               "filename": filename, "mimeType": mime, "size": len(data)}
        if lien:
            out["url"] = media_store.presign_get(media_store.cle_transit(prefix, sub, digest))
            out["expires_in"] = media_store.presign_expiry()
    except media_store.MediaError as e:
        raise TransitError(
            f"the file could not be put in transit: storage error ({e.code}).") from None
    return out


def supprimer(ref: object) -> None:
    """Supprime le dépôt désigné par `ref`, sous l'appelant, dès qu'il a servi — la purge
    de `tmp/` reste le filet. `TransitError` si la référence est mal formée ou si le
    stockage refuse : à l'appelant de le journaliser."""
    prefix, digest = _decomposer(ref)
    sub = access.current_user_sub_or_raise()
    try:
        media_store.delete_transit(prefix, sub, digest)
    except media_store.MediaError as e:
        raise TransitError(
            f"the transit file could not be deleted: storage error ({e.code}).") from None


def _decomposer(ref: object) -> tuple[str, str]:
    """`(préfixe, empreinte)` d'une référence de forme stricte, ou `TransitError`."""
    m = _FORME.fullmatch(ref) if isinstance(ref, str) else None
    if m is None or m.group(1) not in media_store.PREFIXES_TRANSIT:
        raise TransitError(
            "source transit: `ref` must be the reference returned by the tool that "
            "served the file (`<prefix>/<32 hex characters>`), copied as is.")
    return m.group(1), m.group(2)


def lire(ref: object, *, max_bytes: int) -> tuple[bytes, Optional[str], str]:
    """`(octets, nom d'origine, type)` de la référence `ref`, si elle est de l'appelant."""
    prefix, digest = _decomposer(ref)
    sub = access.current_user_sub_or_raise()
    try:
        objet = media_store.lire_objet(media_store.cle_transit(prefix, sub, digest),
                                       max_bytes=max_bytes)
    except media_store.MediaError as e:
        if e.code == "not_found":
            raise TransitError(
                "source transit: this reference has expired (transit files are purged "
                "after one day) or was not issued to you — re-read the file (e.g. the "
                "recording) from the tool that served it, and pass the new `source`."
            ) from None
        if e.code == "object_too_large":
            raise TransitError(f"source transit: file larger than {max_bytes} bytes.") from None
        raise TransitError(f"source transit: storage unreadable ({e.code}).") from None
    return objet.data, objet.filename, objet.content_type

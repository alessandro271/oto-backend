"""Le rechiffrement des secrets d'un export : de la clé source à la clé cible, en mémoire.

Trois colonnes portent une valeur chiffrée avec la clé maîtresse de l'instance
(`classement.secrets`). Leur AAD lie le chiffré à SA ligne, et c'est la fonction du
code qui l'écrit qui la dit — jamais une recopie ici :

- `connector_credentials.secret_enc` : `credentials_store._aad(entity_type, entity_id,
  connector, account)` — l'`entity_id` d'un compte ou d'un membre CONTIENT le sub,
  donc un sub dénudé à l'import (le tenant devient primaire) change l'AAD : on
  déchiffre avec l'AAD de la ligne SOURCE, on rechiffre avec celle de la ligne CIBLE ;
- `runner_triggers.hook_signing_secret_enc` : `runner_hook._aad_du_secret(id)` ;
- `transcription_jobs.api_key_enc` : `transcription_worker._aad(audio_key)`.
  Pour ces deux-là les identifiants sont préservés, l'AAD ne bouge pas.

⚠️ Le clair ne vit que le temps d'un appel : ni fichier, ni journal, ni exception qui
le porterait (`decrypt_with_key` lève sans le citer).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .. import credentials_store, runner_hook, transcription_worker
from ..crypto import decrypt_with_key, encrypt_with_key


@dataclass(frozen=True)
class Cles:
    """Les deux clés maîtresses, 32 octets chacune : celle de la source, celle de la cible."""
    source: bytes
    cible: bytes

    def __post_init__(self):
        if len(self.source) != 32 or len(self.cible) != 32:
            raise ValueError("une clé maîtresse fait 32 octets (AES-256)")

    def __repr__(self) -> str:  # jamais une clé dans une trace
        return "Cles(<masquées>)"


#: table → (colonne chiffrée, AAD de la ligne).
AAD: dict[str, tuple[str, Callable[[dict], str]]] = {
    "connector_credentials": ("secret_enc", lambda l: credentials_store._aad(
        l["entity_type"], l["entity_id"], l["connector"], l["account"] or "")),
    "runner_triggers": ("hook_signing_secret_enc",
                        lambda l: runner_hook._aad_du_secret(l["id"])),
    "transcription_jobs": ("api_key_enc", lambda l: transcription_worker._aad(l["audio_key"])),
}


class RechiffrementImpossible(RuntimeError):
    pass


def rechiffrer(table: str, source: dict, cible: dict, cles: Cles) -> None:
    """Remplace dans `cible` la valeur chiffrée de `table` par son rechiffrement :
    déchiffrée sous la clé source et l'AAD de la ligne `source`, rechiffrée sous la
    clé cible et l'AAD de la ligne `cible` (celle qui sera écrite)."""
    colonne, aad = AAD[table]
    enveloppe = source.get(colonne)
    if enveloppe is None:
        return
    try:
        clair = decrypt_with_key(cles.source, enveloppe, aad(source))
    except RuntimeError as e:
        raise RechiffrementImpossible(
            f"{table} : une valeur ne se déchiffre pas sous la clé source (clé erronée, "
            "ou ligne dont l'identité a bougé)") from e
    cible[colonne] = encrypt_with_key(cles.cible, clair, aad(cible))

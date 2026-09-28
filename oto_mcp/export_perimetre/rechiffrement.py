"""Le rechiffrement des secrets d'un export : CHEZ NOUS, À L'EXPORT (décision du 28/09/2026).

Notre clé maîtresse ne sort jamais de notre infrastructure. Le processus d'export reçoit
la clé de l'instance CIBLE pour cette seule exécution, déchiffre chaque secret sous
notre clé, le rechiffre sous la sienne, et le fichier ne porte QUE des secrets chiffrés
pour la cible. L'import ne connaît que la clé de son instance ; le manifeste dit sous
quelle clé les secrets sont chiffrés par une EMPREINTE (`empreinte_cle`), jamais la clé.

Trois colonnes portent une valeur chiffrée (`classement.secrets`). Leur AAD lie le
chiffré à SA ligne, et c'est la fonction du code qui l'écrit qui la dit — jamais une
recopie ici :

- `connector_credentials.secret_enc` : `credentials_store._aad(entity_type, entity_id,
  connector, account)` — l'`entity_id` d'un compte ou d'un membre CONTIENT le sub, qui
  change à l'import : on déchiffre sous l'AAD de la ligne SOURCE, on rechiffre sous
  celle de la ligne CIBLE (`transformation`, la fonction même de l'import) ;
- `runner_triggers.hook_signing_secret_enc` : `runner_hook._aad_du_secret(id)` ;
- `transcription_jobs.api_key_enc` : `transcription_worker._aad(audio_key)`.

⚠️ Le clair ne vit que le temps d'un appel : ni fichier, ni journal, ni exception qui
le porterait (`decrypt_with_key` lève sans le citer).
"""
from __future__ import annotations

import hashlib
from typing import Callable

from .. import credentials_store, runner_hook, transcription_worker
from ..crypto import decrypt_with_key, encrypt_with_key

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


def empreinte_cle(cle: bytes) -> str:
    """Ce qui désigne une clé maîtresse sans la révéler : un haché à séparation de domaine
    (la clé est 256 bits d'aléa : rien à deviner à partir de son haché)."""
    return hashlib.sha256(b"oto-export-perimetre/cle:" + cle).hexdigest()


def rechiffrer(table: str, source: dict, cible: dict, cle_source: bytes,
               cle_cible: bytes) -> str | None:
    """L'enveloppe du secret de `table`, déchiffrée sous `cle_source` et l'AAD de la
    ligne `source`, rechiffrée sous `cle_cible` et l'AAD de la ligne `cible` (celle que
    l'import écrira)."""
    colonne, aad = AAD[table]
    enveloppe = source.get(colonne)
    if enveloppe is None:
        return None
    try:
        clair = decrypt_with_key(cle_source, enveloppe, aad(source))
    except RuntimeError as e:
        raise RechiffrementImpossible(
            f"{table} : une valeur ne se déchiffre pas sous la clé de cette instance "
            "(enveloppe corrompue, ou ligne dont l'identité a bougé)") from e
    return encrypt_with_key(cle_cible, clair, aad(cible))


def lisible(table: str, ligne: dict, cle: bytes) -> bool:
    """Le secret de `ligne` se déchiffre-t-il sous `cle` et l'AAD de cette ligne ?"""
    colonne, aad = AAD[table]
    if ligne.get(colonne) is None:
        return True
    try:
        decrypt_with_key(cle, ligne[colonne], aad(ligne))
    except RuntimeError:
        return False
    return True

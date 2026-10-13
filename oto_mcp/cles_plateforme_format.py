"""Remettre au format du coffre les clés plateforme posées BRUTES.

Jusqu'à ce que `POST /api/admin/platform-keys` passe par `credentials_store.preparer_pose`,
une clé plateforme était stockée telle que l'opérateur l'avait collée. Pour un connecteur
dont le chiffré porte plusieurs champs (`forager` : clé + compte ; `transcription`), ce
n'est pas le JSON que `unpack_secret` relit. Ce module réécrit ces lignes au format,
une fois, pour que la lecture n'ait plus rien à rattraper.

Joué par `oto-mcp maintenance cles-plateforme-au-format` : À BLANC par défaut, `--apply`
pour écrire. Trois propriétés :
- **rien n'est réécrit si une ligne est illisible** : toutes les lignes sont déchiffrées
  et jugées AVANT la première écriture, et la première illisible lève ;
- **idempotent** : une ligne déjà au format n'est pas touchée ;
- **aucune valeur n'est affichée** : le rapport ne porte que connecteur, libellé, compte
  et verdict.

Seul le chiffré change (même AAD, `version` + 1) : `meta`, `set_by`, `set_at` et les
accords (`share_mode`, `share_down`) restent ceux de la pose d'origine.
"""
from __future__ import annotations

from typing import Optional

from . import credentials_store as cs
from . import crypto, providers
from .db import _connect

AU_FORMAT = "au format"
A_REECRIRE = "à réécrire"
REECRITE = "réécrite"


class CleIllisible(RuntimeError):
    """Une clé plateforme ne se déchiffre pas, ou ne porte aucune valeur à ranger : rien
    n'a été réécrit. À reposer depuis la console."""


def connecteurs_multi_champs() -> list[str]:
    """Les connecteurs dont le chiffré porte plusieurs champs — ceux qu'une valeur brute
    ne remplit pas. `basic_auth` a son propre encodage, hors de cette question."""
    return sorted(n for n, c in providers.REGISTRY.items()
                  if c.credential_of is None and c.secret_kind != "basic_auth"
                  and len(c.vault_fields) >= 2)


def blob_au_format(connector: str, clair: str) -> Optional[str]:
    """`None` si `clair` se relit déjà au format du connecteur ; sinon le blob à écrire,
    la valeur brute rangée sous le premier champ secret (la règle de `preparer_pose`)."""
    try:
        cs.unpack_secret(connector, clair)
        return None
    except cs.SecretUnpackError:
        pass
    valeur = (clair or "").strip()
    if not valeur:
        raise CleIllisible(f"clé plateforme `{connector}` vide : rien à ranger.")
    c = providers.REGISTRY[connector]
    champ = next((f.name for f in c.vault_fields if f.secret), c.vault_fields[0].name)
    return cs.pack_secret(connector, {champ: valeur})


def remettre_au_format(*, dry_run: bool = True) -> dict:
    connecteurs = connecteurs_multi_champs()
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT entity_id, connector, account, secret_enc, version "
            "FROM connector_credentials WHERE entity_type = %s AND connector = ANY(%s) "
            "ORDER BY connector, entity_id, account",
            (cs.PLATFORM, connecteurs)).fetchall()
        plan = []
        for r in lignes:
            aad = cs._aad(cs.PLATFORM, r["entity_id"], r["connector"], r["account"])
            try:
                clair = crypto.decrypt(r["secret_enc"], aad)
            except Exception as e:
                raise CleIllisible(
                    f"clé plateforme `{r['connector']}` « {r['entity_id']} » : le chiffré "
                    f"ne se déchiffre pas ({type(e).__name__}). Rien n'a été réécrit ; "
                    f"reposer cette clé depuis la console.") from None
            plan.append((r, aad, blob_au_format(r["connector"], clair)))
        cles = []
        with conn.transaction():
            for r, aad, neuf in plan:
                if neuf is None:
                    verdict = AU_FORMAT
                elif dry_run:
                    verdict = A_REECRIRE
                else:
                    cur = conn.execute(
                        "UPDATE connector_credentials SET secret_enc = %s, "
                        "version = version + 1 WHERE entity_type = %s AND entity_id = %s "
                        "AND connector = %s AND account = %s AND version = %s",
                        (crypto.encrypt(neuf, aad), cs.PLATFORM, r["entity_id"],
                         r["connector"], r["account"], r["version"]))
                    if (cur.rowcount or 0) != 1:
                        raise cs.ConcurrencyConflict(
                            f"clé plateforme `{r['connector']}` « {r['entity_id']} » "
                            f"modifiée pendant la remise au format — rien n'a été "
                            f"réécrit, relancer.")
                    verdict = REECRITE
                cles.append({"connector": r["connector"], "label": r["entity_id"],
                             "account": r["account"], "verdict": verdict})
    return {"applied": not dry_run, "cles": cles}

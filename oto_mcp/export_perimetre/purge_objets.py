"""Retirer du stockage les objets d'un tenant purgé, versions non courantes comprises.

Les objets se désignent depuis la base (`purge.cles_d_objet`) : des CLÉS (fichiers de
projet, d'app, audios, pièces jointes, avatars et logos cités par URL) et des PRÉFIXES
(`images/<sub>/`, les images qu'un agent a déposées et que seule une URL dans un contenu
désigne). Le relevé se fait AVANT la purge de la base, et se garde : après, plus rien ne
les désigne.

Le seau peut être versionné : supprimer la version courante laisserait les précédentes.
L'inventaire liste donc TOUTES les versions (et marqueurs de suppression) de chaque clé
exacte et de chaque préfixe, la suppression les retire une à une, et le contrôle relit
l'inventaire, qui doit être vide.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    cle: str
    version: str | None
    octets: int


def inventaire(s3, bucket: str, cles: set[str], prefixes: set[str]) -> list[Version]:
    """Toutes les versions des objets désignés : clé exacte, ou sous un préfixe."""
    out: dict[tuple[str, str | None], Version] = {}

    def lister(prefixe: str, exacte: bool) -> None:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket,
                                                                       Prefix=prefixe):
            for v in [*page.get("Versions", ()), *page.get("DeleteMarkers", ())]:
                if exacte and v["Key"] != prefixe:
                    continue
                vid = v.get("VersionId")
                out[(v["Key"], vid)] = Version(v["Key"], vid, int(v.get("Size") or 0))

    for cle in sorted(cles):
        lister(cle, exacte=True)
    for prefixe in sorted(prefixes):
        lister(prefixe, exacte=False)
    return sorted(out.values(), key=lambda v: (v.cle, v.version or ""))


def resume(versions: list[Version]) -> dict:
    return {"objets": len({v.cle for v in versions}), "versions": len(versions),
            "octets": sum(v.octets for v in versions)}


def supprimer(s3, bucket: str, versions: list[Version]) -> int:
    """Supprime chaque version. Rejouable : une version déjà partie ne coûte rien."""
    for v in versions:
        if v.version is None:
            s3.delete_object(Bucket=bucket, Key=v.cle)
        else:
            s3.delete_object(Bucket=bucket, Key=v.cle, VersionId=v.version)
    return len(versions)

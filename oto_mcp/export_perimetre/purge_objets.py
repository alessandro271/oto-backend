"""Retirer du stockage les objets d'un tenant purgé, versions non courantes comprises.

Une clé du stockage est `<famille>/<propriétaire>/…` (`media_store`) ; les objets d'un
tenant sont donc ceux des PRÉFIXES de ses propriétaires (`purge.objets`) :
`avatars/<sub>/`, `images/<sub>/`, `copilot-attachments/<sub>/`, `org-logos/<org>/`,
`project-files/<projet>/`, `transcription-jobs/<projet>/`, `app-files/<app>/`. Le `/`
final est exigé : `project-files/12/` ne touche pas `project-files/123/`. Les préfixes se
relèvent AVANT la purge de la base et se gardent : après, plus rien ne les désigne.

Le seau peut être versionné (`Enabled` ou `Suspended`) : supprimer sans versionId ne
ferait que poser un marqueur de suppression et laisserait les données. L'inventaire liste
donc TOUTES les versions (et marqueurs de suppression) sous chaque préfixe, la suppression
les retire une à une PAR LEUR VERSIONID (`null` compris, ce qui retire vraiment la version
`null`), et le contrôle relit l'inventaire, qui doit être vide. Une version listée sans
versionId est refusée avant toute suppression.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    cle: str
    version: str | None
    octets: int


def _exiger_prefixes(prefixes: set[str]) -> None:
    mauvais = sorted(p for p in prefixes if not p.endswith("/") or p.count("/") < 2)
    if mauvais:
        raise ValueError(f"préfixes sans propriétaire ou sans `/` final : {mauvais}")


def inventaire(s3, bucket: str, prefixes: set[str]) -> list[Version]:
    """Toutes les versions des objets sous les préfixes de propriétaire."""
    _exiger_prefixes(prefixes)
    out: dict[tuple[str, str | None], Version] = {}
    for prefixe in sorted(prefixes):
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket,
                                                                       Prefix=prefixe):
            for v in [*page.get("Versions", ()), *page.get("DeleteMarkers", ())]:
                vid = v.get("VersionId")
                out[(v["Key"], vid)] = Version(v["Key"], vid, int(v.get("Size") or 0))
    return sorted(out.values(), key=lambda v: (v.cle, v.version or ""))


def resume(versions: list[Version]) -> dict:
    par_famille: dict[str, int] = {}
    for v in versions:
        famille = v.cle.split("/", 1)[0]
        par_famille[famille] = par_famille.get(famille, 0) + 1
    return {"objets": len({v.cle for v in versions}), "versions": len(versions),
            "octets": sum(v.octets for v in versions), "versions_par_famille": par_famille}


def supprimer(s3, bucket: str, versions: list[Version]) -> int:
    """Supprime chaque version par son versionId. Rejouable : une version déjà partie ne
    coûte rien."""
    sans_id = sorted(v.cle for v in versions if v.version is None)
    if sans_id:
        raise ValueError(f"versions listées sans versionId, rien n'est supprimé : {sans_id}")
    for v in versions:
        s3.delete_object(Bucket=bucket, Key=v.cle, VersionId=v.version)
    return len(versions)

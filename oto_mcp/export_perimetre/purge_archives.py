"""Retirer un tenant purgé des archives froides du journal d'appels.

Les mois sortis de la rétention vivent sur l'Object Storage, un CSV gzip par mois
(`deploy/archive_tool_calls.py`, registre `journal_archives`). Une archive mêle tous les
propriétaires : elle ne se supprime pas, elle se RÉÉCRIT sans les lignes du tenant.

Une ligne est du tenant si son `sub` ou son `effective_sub` porte le préfixe du tenant
(`<slug>:`), ou si son `org_id` est une org de son périmètre — le périmètre de la purge en
base, `purge.lire`, une seule définition.

Trois temps, séparés et explicites :

1. `compter` (à blanc) : par mois, le total, la part du tenant, le reste, et les versions
   non courantes de l'objet. Le total doit égaler `journal_archives.lignes`, sinon
   `ArchiveDiscordante` — on ne réécrit pas une archive qu'on ne sait pas relire juste ;
2. `extraire` : la part du tenant, un CSV gzip par mois, pour la lui remettre ;
3. `reecrire` : sous la MÊME clé, l'archive sans le tenant ; relue depuis le stockage et
   recomptée (part du tenant 0, reste égal au compte à blanc), puis le registre est mis à
   jour, puis les versions non courantes de la clé sont supprimées. Il exige l'extraction
   du temps 2, dont il recompte les fichiers : la part remise est celle qu'on retire.

Les enregistrements sont recopiés OCTET pour octet : un CSV relu puis réécrit par le
module `csv` confondrait une valeur NULL (champ vide) et une chaîne vide (`""`). Chaque
enregistrement est lu dans son texte brut, borné par la parité des guillemets (le CSV de
`COPY` double les guillemets internes), puis décodé seulement pour être classé.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterator

ESPACE_LIBRE_MIN = 2 * 1024**3


class ArchiveDiscordante(RuntimeError):
    """Une archive ne se relit pas comme le registre le dit, ou une étape ne retombe pas
    sur ses comptes : rien de plus n'est fait."""


@dataclass(frozen=True)
class Tenant:
    prefixe: str                 # `<slug>:`
    orgs: frozenset[str]         # ids d'org, en texte, tels que le CSV les porte


@dataclass
class Mois:
    mois: str
    cle: str
    inscrites: int
    total: int = 0
    tenant: int = 0
    versions_anciennes: int = 0

    @property
    def reste(self) -> int:
        return self.total - self.tenant


def enregistrements(flux: io.TextIOBase) -> Iterator[tuple[str, list[str]]]:
    """(texte brut, champs) de chaque enregistrement d'un CSV de `COPY`, en flux."""
    limite_avant = csv.field_size_limit(10_000_000)
    try:
        tampon: list[str] = []
        guillemets = 0
        for ligne in flux:
            tampon.append(ligne)
            guillemets += ligne.count('"')
            if guillemets % 2:
                continue
            brut = "".join(tampon)
            tampon, guillemets = [], 0
            yield brut, next(csv.reader(io.StringIO(brut)))
        if tampon:
            raise ArchiveDiscordante("enregistrement final tronqué (guillemet non fermé)")
    finally:
        csv.field_size_limit(limite_avant)


def _classeur(entete: list[str], tenant: Tenant) -> Callable[[list[str]], bool]:
    try:
        i_sub, i_eff, i_org = (entete.index(c) for c in ("sub", "effective_sub", "org_id"))
    except ValueError as e:
        raise ArchiveDiscordante(f"colonne absente de l'archive : {e}") from e

    def du_tenant(champs: list[str]) -> bool:
        return (champs[i_sub].startswith(tenant.prefixe)
                or champs[i_eff].startswith(tenant.prefixe)
                or champs[i_org] in tenant.orgs)
    return du_tenant


def _parcourir(corps, tenant: Tenant) -> Iterator[tuple[str, bool | None]]:
    """(texte brut, est-il du tenant) — l'en-tête d'abord, avec `None`."""
    with gzip.GzipFile(fileobj=corps) as gz:
        flux = enregistrements(io.TextIOWrapper(gz, encoding="utf-8", newline=""))
        premier = next(flux, None)
        if premier is None:
            raise ArchiveDiscordante("archive vide ou illisible")
        brut, entete = premier
        du_tenant = _classeur(entete, tenant)
        yield brut, None
        for brut, champs in flux:
            yield brut, du_tenant(champs)


def _compter(corps, tenant: Tenant) -> tuple[int, int]:
    total = part = 0
    for _, du in _parcourir(corps, tenant):
        if du is None:
            continue
        total += 1
        part += du
    return total, part


def _versions_anciennes(s3, bucket: str, cle: str) -> list[str]:
    ids: list[str] = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=cle):
        ids += [v["VersionId"] for v in page.get("Versions", ())
                if v["Key"] == cle and not v["IsLatest"]]
    return ids


def registre(conn) -> list[Mois]:
    return [Mois(r["mois"], r["cle"], r["lignes"]) for r in conn.execute(
        "SELECT mois, cle, lignes FROM journal_archives ORDER BY mois")]


def compter(conn, s3, bucket: str, tenant: Tenant) -> list[Mois]:
    """Le compte à blanc, mois par mois. Lève `ArchiveDiscordante` si une archive ne
    porte pas le nombre d'enregistrements que le registre inscrit."""
    out = registre(conn)
    for m in out:
        m.total, m.tenant = _compter(s3.get_object(Bucket=bucket, Key=m.cle)["Body"], tenant)
        if m.total != m.inscrites:
            raise ArchiveDiscordante(f"{m.mois} : {m.total} enregistrements relus dans "
                                     f"{m.cle}, le registre en inscrit {m.inscrites}")
        m.versions_anciennes = len(_versions_anciennes(s3, bucket, m.cle))
    return out


def _nom_extrait(dossier: str, mois: str) -> str:
    return os.path.join(dossier, f"{mois}.csv.gz")


def _compter_fichier(chemin: str, tenant: Tenant) -> tuple[int, int]:
    with open(chemin, "rb") as f:
        return _compter(f, tenant)


def extraire(s3, bucket: str, tenant: Tenant, mois: list[Mois], dossier: str) -> dict[str, int]:
    """Écrit la part du tenant de chaque mois (en-tête compris) dans `dossier`, relit
    chaque fichier et le recompte. Un fichier existant n'est pas écrasé."""
    os.makedirs(dossier, exist_ok=True)
    out: dict[str, int] = {}
    for m in mois:
        chemin = _nom_extrait(dossier, m.mois)
        if os.path.exists(chemin):
            raise ArchiveDiscordante(f"{chemin} existe déjà : une extraction ne s'écrase pas")
        with gzip.open(chemin, "wt", encoding="utf-8", newline="") as sortie:
            for brut, du in _parcourir(s3.get_object(Bucket=bucket, Key=m.cle)["Body"], tenant):
                if du is None or du:
                    sortie.write(brut)
        total, part = _compter_fichier(chemin, tenant)
        if not (total == part == m.tenant):
            raise ArchiveDiscordante(f"{chemin} : {total} enregistrements relus dont {part} "
                                     f"du tenant, pour {m.tenant} comptés")
        out[m.mois] = total
    return out


def reecrire(conn, s3, bucket: str, tenant: Tenant, mois: list[Mois], extrait: str) -> dict:
    """Réécrit chaque mois qui porte des lignes du tenant, sous la même clé, sans elles ;
    relit, recompte, met à jour le registre, supprime les versions non courantes. Exige
    l'extraction (`extraire`) du même compte."""
    for m in mois:
        chemin = _nom_extrait(extrait, m.mois)
        if m.tenant and (not os.path.exists(chemin)
                         or _compter_fichier(chemin, tenant) != (m.tenant, m.tenant)):
            raise ArchiveDiscordante(f"{m.mois} : l'extraction de {extrait} ne porte pas les "
                                     f"{m.tenant} lignes du tenant — la refaire d'abord")
    if shutil.disk_usage(tempfile.gettempdir()).free < ESPACE_LIBRE_MIN:
        raise ArchiveDiscordante(f"moins de 2 Go libres sur {tempfile.gettempdir()}")
    fait: dict[str, dict] = {}
    for m in mois:
        if not m.tenant:
            continue
        fd, chemin = tempfile.mkstemp(prefix=f"tool_calls-{m.mois}-", suffix=".csv.gz")
        os.close(fd)
        try:
            with gzip.open(chemin, "wt", encoding="utf-8", newline="") as sortie:
                for brut, du in _parcourir(s3.get_object(Bucket=bucket, Key=m.cle)["Body"],
                                           tenant):
                    if not du:
                        sortie.write(brut)
            if _compter_fichier(chemin, tenant) != (m.reste, 0):
                raise ArchiveDiscordante(f"{m.mois} : la réécriture locale ne porte pas les "
                                         f"{m.reste} lignes attendues — rien n'est déposé")
            s3.upload_file(chemin, bucket, m.cle)  # pas d'ACL : objet privé
        finally:
            os.unlink(chemin)
        relu = _compter(s3.get_object(Bucket=bucket, Key=m.cle)["Body"], tenant)
        if relu != (m.reste, 0):
            raise ArchiveDiscordante(
                f"{m.mois} : {m.cle} relu à {relu[0]} enregistrements dont {relu[1]} du "
                f"tenant, pour {m.reste} et 0 attendus — la version précédente est gardée")
        if conn.execute("UPDATE journal_archives SET lignes = %s WHERE mois = %s AND lignes = %s",
                        (m.reste, m.mois, m.inscrites)).rowcount != 1:
            raise ArchiveDiscordante(f"{m.mois} : le registre a changé pendant la réécriture")
        anciennes = _versions_anciennes(s3, bucket, m.cle)
        for v in anciennes:
            s3.delete_object(Bucket=bucket, Key=m.cle, VersionId=v)
        fait[m.mois] = {"retirees": m.tenant, "reste": m.reste,
                        "versions_supprimees": len(anciennes)}
    return fait

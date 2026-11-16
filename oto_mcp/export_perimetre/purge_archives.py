"""Retirer un tenant purgé des archives froides du journal d'appels.

Les mois sortis de la rétention vivent sur l'Object Storage, un CSV gzip par mois
(`deploy/archive_tool_calls.py`, registre `journal_archives`). Une archive mêle tous les
propriétaires : elle ne se supprime pas, elle se RÉÉCRIT sans les lignes du tenant.

Une ligne est du tenant selon EXACTEMENT la règle de la base pour `tool_calls`
(`purge.JOURNAL_REGLE`, compilée en Python par `purge.classeur_du_journal`, sur le
périmètre figé) : les archives retirent ce que la base supprime, ni plus ni moins.

Trois temps, séparés et explicites :

1. `compter` (à blanc) : par mois, le total, la part du tenant, le reste, et les versions
   non courantes de l'objet. Le total doit égaler `journal_archives.lignes`, sinon
   `ArchiveDiscordante` — on ne réécrit pas une archive qu'on ne sait pas relire juste ;
2. `extraire` : la part du tenant, un CSV gzip par mois, pour la lui remettre ;
3. `reecrire` : sous la MÊME clé, l'archive sans le tenant ; relue depuis le stockage et
   recomptée (part du tenant 0, reste égal au compte à blanc), puis le registre est mis à
   jour, puis les versions non courantes de la clé sont supprimées. Il exige l'extraction
   du temps 2, dont il recompte les fichiers : la part remise est celle qu'on retire.

Le seau doit être VERSIONNÉ (`exiger_versionnement`) : la version précédente d'une archive
reste le filet tant que la réécriture n'est pas relue juste. Tout se REPREND : une
extraction complète déjà là est reprise (recomptée) ; un mois dont l'objet courant est
déjà la version purgée (part 0, total = inscrit − part extraite) mais dont le registre n'a
pas suivi est reconnu (`registre_en_retard`) et seul son registre est mis à jour.

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
    """La règle du journal compilée sur le périmètre figé (`purge.classeur_du_journal`)."""
    colonnes: tuple[str, ...]
    du_tenant: Callable[[dict[str, str]], bool]


@dataclass
class Mois:
    mois: str
    cle: str
    inscrites: int
    total: int = 0
    tenant: int = 0
    versions_anciennes: int = 0
    registre_en_retard: bool = False    # objet déjà purgé, registre pas encore suivi

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
        index = {c: entete.index(c) for c in tenant.colonnes}
    except ValueError as e:
        raise ArchiveDiscordante(f"colonne absente de l'archive : {e}") from e

    def du_tenant(champs: list[str]) -> bool:
        return tenant.du_tenant({c: champs[i] for c, i in index.items()})
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


def exiger_versionnement(s3, bucket: str) -> None:
    statut = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    if statut != "Enabled":
        raise ArchiveDiscordante(f"le seau {bucket!r} n'est pas versionné ({statut!r}) : une "
                                 "réécriture n'aurait pas de filet — refus")


def compter(conn, s3, bucket: str, tenant: Tenant, extrait: str | None = None) -> list[Mois]:
    """Le compte à blanc, mois par mois, sur TOUTES les archives du registre. Lève
    `ArchiveDiscordante` si une archive ne porte pas le nombre d'enregistrements que le
    registre inscrit — sauf le cas d'une reprise : l'objet courant est déjà purgé (part 0)
    et l'extraction de `extrait` explique exactement l'écart (`registre_en_retard`)."""
    out = registre(conn)
    for m in out:
        m.total, m.tenant = _compter(s3.get_object(Bucket=bucket, Key=m.cle)["Body"], tenant)
        m.versions_anciennes = len(_versions_anciennes(s3, bucket, m.cle))
        if m.total == m.inscrites:
            continue
        chemin = _nom_extrait(extrait, m.mois) if extrait else None
        if m.tenant == 0 and chemin and os.path.exists(chemin):
            n, part = _compter_fichier(chemin, tenant)
            if n == part and m.total == m.inscrites - n:
                m.registre_en_retard = True
                continue
        raise ArchiveDiscordante(f"{m.mois} : {m.total} enregistrements relus dans "
                                 f"{m.cle}, le registre en inscrit {m.inscrites}")
    return out


def _nom_extrait(dossier: str, mois: str) -> str:
    return os.path.join(dossier, f"{mois}.csv.gz")


def _compter_fichier(chemin: str, tenant: Tenant) -> tuple[int, int]:
    with open(chemin, "rb") as f:
        return _compter(f, tenant)


def extraire(s3, bucket: str, tenant: Tenant, mois: list[Mois], dossier: str) -> dict[str, int]:
    """Écrit la part du tenant de chaque mois (en-tête compris) dans `dossier`, relit
    chaque fichier et le recompte. Un fichier déjà là et complet (il porte exactement la
    part comptée) est repris ; incomplet ou faux, il refuse — jamais écrasé."""
    os.makedirs(dossier, exist_ok=True)
    out: dict[str, int] = {}
    for m in mois:
        chemin = _nom_extrait(dossier, m.mois)
        if os.path.exists(chemin):
            if _compter_fichier_sur(chemin, tenant) == (m.tenant, m.tenant):
                out[m.mois] = m.tenant
                continue
            raise ArchiveDiscordante(f"{chemin} existe et ne porte pas les {m.tenant} lignes "
                                     "du tenant : à examiner, jamais écrasé")
        partiel = chemin + ".partiel"
        with gzip.open(partiel, "wt", encoding="utf-8", newline="") as sortie:
            for brut, du in _parcourir(s3.get_object(Bucket=bucket, Key=m.cle)["Body"], tenant):
                if du is None or du:
                    sortie.write(brut)
        total, part = _compter_fichier(partiel, tenant)
        if not (total == part == m.tenant):
            raise ArchiveDiscordante(f"{partiel} : {total} enregistrements relus dont {part} "
                                     f"du tenant, pour {m.tenant} comptés")
        os.replace(partiel, chemin)
        out[m.mois] = total
    return out


def _compter_fichier_sur(chemin: str, tenant: Tenant) -> tuple[int, int]:
    try:
        return _compter_fichier(chemin, tenant)
    except (OSError, EOFError, ArchiveDiscordante):
        return (-1, -1)


def reecrire(conn, s3, bucket: str, tenant: Tenant, mois: list[Mois], extrait: str) -> dict:
    """Réécrit chaque mois qui porte des lignes du tenant, sous la même clé, sans elles ;
    relit, recompte, met à jour le registre, supprime les versions non courantes. Exige
    l'extraction (`extraire`) du même compte."""
    exiger_versionnement(s3, bucket)
    for m in mois:
        chemin = _nom_extrait(extrait, m.mois)
        if m.tenant and _compter_fichier_sur(chemin, tenant) != (m.tenant, m.tenant):
            raise ArchiveDiscordante(f"{m.mois} : l'extraction de {extrait} ne porte pas les "
                                     f"{m.tenant} lignes du tenant — la refaire d'abord")
    if shutil.disk_usage(tempfile.gettempdir()).free < ESPACE_LIBRE_MIN:
        raise ArchiveDiscordante(f"moins de 2 Go libres sur {tempfile.gettempdir()}")
    fait: dict[str, dict] = {}
    for m in mois:
        if m.registre_en_retard:
            fait[m.mois] = {"retirees": m.inscrites - m.total, "reste": m.total,
                            "versions_supprimees": _finir(conn, s3, bucket, m, m.total),
                            "reprise": "registre"}
            continue
        if not m.tenant:
            # Reprise : le mois a été purgé (son extraction est là), ses versions
            # anciennes, qui portent encore le tenant, n'ont pas toutes été supprimées.
            if m.versions_anciennes and os.path.exists(_nom_extrait(extrait, m.mois)):
                anciennes = _versions_anciennes(s3, bucket, m.cle)
                for v in anciennes:
                    s3.delete_object(Bucket=bucket, Key=m.cle, VersionId=v)
                fait[m.mois] = {"retirees": 0, "reste": m.total,
                                "versions_supprimees": len(anciennes), "reprise": "versions"}
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
        fait[m.mois] = {"retirees": m.tenant, "reste": m.reste,
                        "versions_supprimees": _finir(conn, s3, bucket, m, m.reste)}
    return fait


def _finir(conn, s3, bucket: str, m: Mois, reste: int) -> int:
    """Le registre suit l'objet relu juste, puis les versions anciennes partent."""
    if conn.execute("UPDATE journal_archives SET lignes = %s WHERE mois = %s AND lignes = %s",
                    (reste, m.mois, m.inscrites)).rowcount != 1:
        raise ArchiveDiscordante(f"{m.mois} : le registre a changé pendant la réécriture")
    anciennes = _versions_anciennes(s3, bucket, m.cle)
    for v in anciennes:
        s3.delete_object(Bucket=bucket, Key=m.cle, VersionId=v)
    return len(anciennes)

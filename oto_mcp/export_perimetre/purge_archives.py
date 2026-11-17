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
3. `reecrire` : sous la MÊME clé, l'archive sans le tenant, puis, dans cet ordre : la
   NOUVELLE version, lue par son versionId, est recomptée (part du tenant 0, reste égal au
   compte à blanc) ; le registre est mis à jour ; l'ANCIENNE version, qui porte encore le
   tenant, est supprimée par son versionId. Il exige l'extraction du temps 2, dont il
   recompte les fichiers : la part remise est celle qu'on retire.

L'ancienne version est le filet tant que la nouvelle n'est pas relue juste. Le seau peut
être `Enabled` ou `Suspended` : les deux suivent le même chemin. En `Suspended`, une
écriture crée la version `null` et ne remplace qu'une version `null` : une version au
versionId réel survit, elle sert de filet. Une archive touchée est donc REFUSÉE
(`verdict`) si sa version courante est `null` (l'écriture remplacerait la seule copie),
si la gestion de versions n'a jamais été activée, si elle a plusieurs versions ou un
marqueur de suppression : tout imprévu est un refus nommé, avant toute écriture.

Si la nouvelle version ne se relit pas juste, rien n'est supprimé de l'ancienne : la
nouvelle, ratée, est retirée par son versionId si l'ancienne est relue présente (elle
redevient courante), sinon le refus nomme les deux versionId et le geste à faire.

Tout se REPREND : une extraction complète déjà là est reprise (recomptée) ; un mois dont
l'objet courant est déjà la version purgée (part 0, total = inscrit − part extraite) mais
dont le registre n'a pas suivi est reconnu (`registre_en_retard`) : son registre est mis
à jour et ses versions anciennes sont supprimées par leur versionId.

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
    version: str | None = None          # versionId de la version courante
    refus: str | None = None            # pourquoi une archive touchée ne se réécrit pas

    @property
    def touchee(self) -> bool:
        """Elle porte des lignes du tenant : elle sera réécrite."""
        return self.tenant > 0

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


NULLE = "null"     # le versionId d'une version écrite sans gestion de versions active
STATUTS_AVEC_FILET = ("Enabled", "Suspended")


def statut_versionnement(s3, bucket: str) -> str | None:
    """`Enabled`, `Suspended`, ou `None` : la gestion de versions n'a jamais été activée."""
    return s3.get_bucket_versioning(Bucket=bucket).get("Status")


def _versions(s3, bucket: str, cle: str) -> tuple[list[dict], list[dict]]:
    """(versions, marqueurs de suppression) de la clé exacte."""
    versions: list[dict] = []
    marqueurs: list[dict] = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=cle):
        versions += [v for v in page.get("Versions", ()) if v["Key"] == cle]
        marqueurs += [v for v in page.get("DeleteMarkers", ()) if v["Key"] == cle]
    return versions, marqueurs


def _courante(versions: list[dict]) -> str | None:
    return next((v.get("VersionId") for v in versions if v.get("IsLatest")), None)


def verdict(s3, bucket: str, statut: str | None, cle: str) -> tuple[str | None, str | None]:
    """(versionId courant, raison du refus ou `None`) d'une archive à réécrire."""
    versions, marqueurs = _versions(s3, bucket, cle)
    courante = _courante(versions)
    if statut not in STATUTS_AVEC_FILET:
        return courante, (f"gestion de versions jamais activée sur le seau ({statut!r}) : "
                          "l'écriture remplacerait la seule copie")
    if marqueurs:
        return courante, (f"{len(marqueurs)} marqueur(s) de suppression sur la clé : "
                          "imprévu, à examiner")
    if len(versions) != 1:
        return courante, (f"{len(versions)} versions sur la clé, une seule attendue : "
                          "imprévu, à examiner")
    if courante in (None, NULLE):
        return courante, ("version courante `null` : l'écriture la remplacerait, elle est "
                          "la seule copie")
    return courante, None


def _lire(s3, bucket: str, cle: str, version: str | None):
    """Le corps d'une version précise ; la courante quand le seau n'en nomme aucune."""
    lecture = {"VersionId": version} if version is not None else {}
    return s3.get_object(Bucket=bucket, Key=cle, **lecture)["Body"]


def _anciennes(s3, bucket: str, cle: str) -> list[str]:
    """Les versionId des versions non courantes ; un marqueur ou un versionId absent
    refuse — on ne supprime que ce qu'on nomme."""
    versions, marqueurs = _versions(s3, bucket, cle)
    if marqueurs:
        raise ArchiveDiscordante(f"{cle} : {len(marqueurs)} marqueur(s) de suppression, "
                                 "imprévu — rien n'est supprimé")
    ids = [v.get("VersionId") for v in versions if not v.get("IsLatest")]
    if None in ids:
        raise ArchiveDiscordante(f"{cle} : une version ancienne sans versionId — rien "
                                 "n'est supprimé")
    return ids


def registre(conn) -> list[Mois]:
    return [Mois(r["mois"], r["cle"], r["lignes"]) for r in conn.execute(
        "SELECT mois, cle, lignes FROM journal_archives ORDER BY mois")]


def compter(conn, s3, bucket: str, tenant: Tenant, extrait: str | None = None) -> list[Mois]:
    """Le compte à blanc, mois par mois, sur TOUTES les archives du registre, avec la
    version courante de chacune et, pour une archive touchée, son verdict (`refus`). Lève
    `ArchiveDiscordante` si une archive ne porte pas le nombre d'enregistrements que le
    registre inscrit — sauf le cas d'une reprise : l'objet courant est déjà purgé (part 0)
    et l'extraction de `extrait` explique exactement l'écart (`registre_en_retard`)."""
    statut = statut_versionnement(s3, bucket)
    out = registre(conn)
    for m in out:
        versions, _ = _versions(s3, bucket, m.cle)
        m.version = _courante(versions)
        m.versions_anciennes = sum(1 for v in versions if not v.get("IsLatest"))
        m.total, m.tenant = _compter(_lire(s3, bucket, m.cle, m.version), tenant)
        if m.touchee:
            m.version, m.refus = verdict(s3, bucket, statut, m.cle)
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


def exiger_acceptees(mois: list[Mois]) -> None:
    """Refuse, avant toute écriture, si une archive touchée a un verdict de refus."""
    refus = [f"{m.mois} ({m.cle}, version {m.version}) : {m.refus}"
             for m in mois if m.touchee and m.refus]
    if refus:
        raise ArchiveDiscordante("archives refusées, rien n'est écrit — " + " ; ".join(refus))


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
            for brut, du in _parcourir(_lire(s3, bucket, m.cle, m.version), tenant):
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
    """Réécrit chaque mois touché, sous la même clé, sans le tenant ; relit la nouvelle
    version par son versionId, met à jour le registre, supprime l'ancienne par son
    versionId. Exige l'extraction (`extraire`) du même compte et un verdict accepté pour
    chaque mois touché, revérifié sur le stockage juste avant d'écrire."""
    exiger_acceptees(mois)
    statut = statut_versionnement(s3, bucket)
    for m in mois:
        if not m.touchee:
            continue
        chemin = _nom_extrait(extrait, m.mois)
        if _compter_fichier_sur(chemin, tenant) != (m.tenant, m.tenant):
            raise ArchiveDiscordante(f"{m.mois} : l'extraction de {extrait} ne porte pas les "
                                     f"{m.tenant} lignes du tenant — la refaire d'abord")
        version, refus = verdict(s3, bucket, statut, m.cle)
        if refus or version != m.version:
            raise ArchiveDiscordante(f"{m.mois} : l'archive a changé depuis le compte "
                                     f"(version {m.version} → {version} ; {refus}) — rien "
                                     "n'est écrit")
    if shutil.disk_usage(tempfile.gettempdir()).free < ESPACE_LIBRE_MIN:
        raise ArchiveDiscordante(f"moins de 2 Go libres sur {tempfile.gettempdir()}")
    fait: dict[str, dict] = {}
    for m in mois:
        if m.registre_en_retard:
            _registre_suit(conn, m, m.total)
            fait[m.mois] = {"retirees": m.inscrites - m.total, "reste": m.total,
                            "version_nouvelle": m.version,
                            "versions_supprimees": _supprimer_anciennes(s3, bucket, m.cle),
                            "reprise": "registre"}
            continue
        if not m.touchee:
            # Reprise : le mois a été purgé (son extraction est là), ses versions
            # anciennes, qui portent encore le tenant, n'ont pas toutes été supprimées.
            if m.versions_anciennes and os.path.exists(_nom_extrait(extrait, m.mois)):
                fait[m.mois] = {"retirees": 0, "reste": m.total, "version_nouvelle": m.version,
                                "versions_supprimees": _supprimer_anciennes(s3, bucket, m.cle),
                                "reprise": "versions"}
            continue
        fait[m.mois] = _reecrire_mois(conn, s3, bucket, tenant, m)
    return fait


def _reecrire_mois(conn, s3, bucket: str, tenant: Tenant, m: Mois) -> dict:
    ancienne = m.version
    fd, chemin = tempfile.mkstemp(prefix=f"tool_calls-{m.mois}-", suffix=".csv.gz")
    os.close(fd)
    try:
        with gzip.open(chemin, "wt", encoding="utf-8", newline="") as sortie:
            for brut, du in _parcourir(_lire(s3, bucket, m.cle, ancienne), tenant):
                if not du:
                    sortie.write(brut)
        if _compter_fichier(chemin, tenant) != (m.reste, 0):
            raise ArchiveDiscordante(f"{m.mois} : la réécriture locale ne porte pas les "
                                     f"{m.reste} lignes attendues — rien n'est déposé")
        with open(chemin, "rb") as f:
            nouvelle = s3.put_object(Bucket=bucket, Key=m.cle, Body=f).get("VersionId")
    finally:
        os.unlink(chemin)
    if nouvelle is None or nouvelle == ancienne:
        raise ArchiveDiscordante(f"{m.mois} : le dépôt n'a pas créé de nouvelle version "
                                 f"(ancienne {ancienne}, nouvelle {nouvelle}) — rien n'est "
                                 "supprimé, examiner les versions de la clé")
    try:
        relu = _compter(_lire(s3, bucket, m.cle, nouvelle), tenant)
    except (OSError, EOFError, ArchiveDiscordante) as e:
        relu, detail = None, f"illisible ({type(e).__name__} : {e})"
    else:
        detail = f"relue à {relu[0]} enregistrements dont {relu[1]} du tenant"
    if relu != (m.reste, 0):
        _echec(s3, bucket, m, ancienne, nouvelle,
               f"{detail}, pour {m.reste} et 0 attendus")
    _registre_suit(conn, m, m.reste)
    versions, marqueurs = _versions(s3, bucket, m.cle)
    ids = sorted(v.get("VersionId") for v in versions)
    if marqueurs or ids != sorted([ancienne, nouvelle]) or _courante(versions) != nouvelle:
        raise ArchiveDiscordante(
            f"{m.mois} : versions de {m.cle} inattendues après le dépôt ({ids}, "
            f"{len(marqueurs)} marqueur(s)) ; ancienne {ancienne}, nouvelle {nouvelle} — "
            "l'ancienne, qui porte encore le tenant, n'est pas supprimée : à examiner")
    s3.delete_object(Bucket=bucket, Key=m.cle, VersionId=ancienne)
    return {"retirees": m.tenant, "reste": m.reste, "version_supprimee": ancienne,
            "version_nouvelle": nouvelle}


def _echec(s3, bucket: str, m: Mois, ancienne: str, nouvelle: str, detail: str) -> None:
    """La nouvelle version ne se relit pas juste : l'ancienne est gardée. La nouvelle est
    retirée par son versionId seulement si l'ancienne est relue présente et redevient
    courante ; sinon le refus dit quoi faire."""
    base = (f"{m.mois} : la nouvelle version {nouvelle} de {m.cle} est {detail} — "
            f"l'ancienne version {ancienne} est gardée")
    try:
        s3.head_object(Bucket=bucket, Key=m.cle, VersionId=ancienne)
    except Exception as e:  # noqa: BLE001 — tout échec de relecture interdit le retrait
        raise ArchiveDiscordante(
            f"{base} ; elle ne se relit pas ({type(e).__name__}), la nouvelle n'est donc "
            f"PAS retirée : examiner les versions de la clé, puis retirer {nouvelle} par "
            "son versionId une fois l'ancienne retrouvée") from e
    s3.delete_object(Bucket=bucket, Key=m.cle, VersionId=nouvelle)
    versions, _ = _versions(s3, bucket, m.cle)
    if _courante(versions) != ancienne:
        raise ArchiveDiscordante(f"{base} ; la nouvelle a été retirée mais la version "
                                 f"courante est {_courante(versions)} : à examiner")
    raise ArchiveDiscordante(f"{base} et redevient courante ; la nouvelle, ratée, a été "
                             "retirée par son versionId. Rien d'autre n'a changé.")


def _registre_suit(conn, m: Mois, reste: int) -> None:
    if conn.execute("UPDATE journal_archives SET lignes = %s WHERE mois = %s AND lignes = %s",
                    (reste, m.mois, m.inscrites)).rowcount != 1:
        raise ArchiveDiscordante(f"{m.mois} : le registre a changé pendant la réécriture")


def _supprimer_anciennes(s3, bucket: str, cle: str) -> list[str]:
    """Supprime chaque version non courante par son versionId ; rend leurs versionId."""
    ids = _anciennes(s3, bucket, cle)
    for vid in ids:
        s3.delete_object(Bucket=bucket, Key=cle, VersionId=vid)
    return ids

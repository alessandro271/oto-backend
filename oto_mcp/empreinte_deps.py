"""Les dépendances **INSTALLÉES**, et leur conformité au verrou (oto-backend#932).

Mesuré le 11/09 puis le 30/09/2026 : quatre arbres de déploiement, quatre jeux de
dépendances différents. `pip install -e .` dans un venv qui existe déjà ne met jamais à
jour une dépendance déjà satisfaite : chaque couleur gardait ce qu'elle avait résolu le
jour de sa création, et une bascule bleu/vert pouvait changer le SDK MCP de la
production sans qu'aucun commit ne le dise. Le constat avait demandé d'aller lire
quatre venv à la main. Ce module en fait un fait SERVI par `GET /api/version` :

- `deps_sha` — l'empreinte de ce qui est installé : sha256 des lignes `nom==version`
  (nom canonique PEP 503), triées, jointes par `\\n`, sans pip/setuptools/wheel quand
  le verrou ne les prescrit pas. Deux arbres qui servent le même sha portent le même
  jeu, au paquet près ;
- `lock_sha` — le sha256 de l'`uv.lock` de l'arbre, octet pour octet : QUEL verrou
  l'arbre porte. `null` quand l'arbre n'en a pas (une image installée en wheel) ;
- `deps_conformes` — ce qui est installé est-il EXACTEMENT ce que `uv sync --frozen`
  poserait depuis ce verrou, pour cet interpréteur ? Un paquet en trop (installé à la
  main, laissé par une ancienne résolution), un paquet manquant ou une version
  différente rendent `false`, et les écarts partent au journal une fois, au boot.

**Ce que « le verrou prescrit » veut dire.** L'`uv.lock` est universel : il porte les
paquets de toutes les plateformes, de toutes les versions de Python admises et de tous
les extras. On le parcourt donc comme `uv sync` : depuis la racine (le projet, source
`editable = "."`), en suivant les dépendances dont le marqueur est vrai POUR CET
INTERPRÉTEUR, extras demandés compris. Les extras propres au projet (`dev`) ne se
devinent pas : l'installé est conforme s'il égale la fermeture pour UN des jeux
d'extras déclarés — sans extra (le service), ou avec `dev` (la CI).

**Calculé une fois**, au premier appel — donc au boot, puisque l'étiquette de version
est résolue au montage de l'application. Un déploiement qui réinstalle l'arbre d'une
autre couleur ne change pas ce qu'annonce un processus déjà démarré.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import re
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Iterable, Optional

from packaging.markers import Marker
from packaging.version import InvalidVersion, Version

if sys.version_info >= (3, 11):
    import tomllib
else:                                       # la box tourne en 3.10 (deploy.yml)
    import tomli as tomllib

log = logging.getLogger(__name__)

FICHIER_VERROU = "uv.lock"

#: Les paquets d'AMORÇAGE d'un venv (`python -m venv` les pose). `uv sync --frozen` ne
#: les retire jamais, même en synchronisation exacte (mesuré le 30/09/2026 avec
#: uv 0.12.19 : un paquet hors verrou est retiré, pip et setuptools restent). Absents du
#: verrou, ils ne sont donc pas un écart ; prescrits par lui, leur version compte.
#: Même règle pour `deps_sha` : ils n'y entrent que si le verrou les prescrit — sinon
#: deux couleurs au même verrou, dont les venv ont été amorcés à des dates différentes,
#: auraient deux empreintes pour le même jeu.
AMORCAGE = frozenset({"pip", "setuptools", "wheel"})

#: Nombre d'écarts écrits au journal : assez pour nommer la dérive, pas un roman.
_ECARTS_AU_JOURNAL = 20


class VerrouIllisible(ValueError):
    """L'`uv.lock` existe mais ne se parcourt pas : ni racine, ni référence résoluble.
    Une erreur, jamais une conformité supposée — `uv sync --frozen` aurait déjà refusé
    le même fichier au déploiement."""


def nom_canonique(nom: str) -> str:
    """PEP 503 : `jaraco.classes`, `Jaraco_Classes` et `jaraco-classes` sont un seul
    paquet. `pip freeze` et `uv.lock` n'écrivent pas les noms pareil."""
    return re.sub(r"[-_.]+", "-", nom).lower()


def _meme_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


# ── ce qui est installé ──────────────────────────────────────────────────────

def installees(distributions: Optional[Iterable] = None) -> dict[str, set[str]]:
    """{nom canonique : versions installées}. Un ensemble et pas une valeur : deux
    `dist-info` du même paquet sur le chemin (une installation interrompue) sont un
    écart en soi, qu'une valeur unique cacherait."""
    out: dict[str, set[str]] = {}
    for dist in (metadata.distributions() if distributions is None else distributions):
        nom = dist.metadata["Name"]
        if not nom:
            # Un dist-info sans nom est une installation cassée : on le COMPTE (il
            # rend l'arbre non conforme) au lieu de l'ignorer.
            nom = "(dist-info-sans-nom)"
        out.setdefault(nom_canonique(nom), set()).add(dist.version or "?")
    return out


def commits_vcs(distributions: Iterable) -> dict[str, set[str]]:
    """{nom canonique : commits} des paquets installés DEPUIS GIT, lus dans
    `direct_url.json` (PEP 610). Le numéro de version ne suffit pas pour eux : oto-core
    a longtemps servi le même `Version` pour des tags différents (`version.py`)."""
    out: dict[str, set[str]] = {}
    for dist in distributions:
        brut = dist.read_text("direct_url.json")
        if not brut or not dist.metadata["Name"]:
            continue
        try:
            charge = json.loads(brut)
            vcs = charge.get("vcs_info") if isinstance(charge, dict) else None
            commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
        except ValueError:
            # Illisible : on le COMPTE comme un commit inconnu, qui ne sera égal à
            # aucun commit du verrou — jamais comme une installation conforme.
            commit = "direct_url.json illisible"
        if commit:
            out.setdefault(nom_canonique(dist.metadata["Name"]), set()).add(commit)
    return out


def empreinte(paquets: dict[str, set[str]],
              amorcage_prescrit: frozenset[str] = frozenset()) -> str:
    """sha256 des lignes `nom==version`, triées, jointes par `\\n` — sans les paquets
    d'amorçage (`AMORCAGE`) que le verrou ne prescrit pas (`amorcage_prescrit`).

    Se recalcule hors du serveur, sur n'importe quel arbre (`etat` fait le reste) :
    `.venv/bin/python -c "from pathlib import Path; from oto_mcp import empreinte_deps as e; print(e.etat(Path('.'))['deps_sha'])"`.
    """
    lignes = sorted(f"{nom}=={v}" for nom, versions in paquets.items() for v in versions
                    if nom not in AMORCAGE or nom in amorcage_prescrit)
    return hashlib.sha256("\n".join(lignes).encode("utf-8")).hexdigest()


# ── ce que le verrou prescrit ────────────────────────────────────────────────

def _racine(paquets: list[dict]) -> dict:
    racines = [p for p in paquets
               if (p.get("source") or {}).get("editable") == "."
               or (p.get("source") or {}).get("virtual") == "."]
    if len(racines) != 1:
        raise VerrouIllisible(
            f"{len(racines)} paquet(s) de source « . » dans le verrou — il en faut "
            "exactement un, le projet")
    return racines[0]


def _vrai(marqueur: Optional[str]) -> bool:
    return marqueur is None or Marker(marqueur).evaluate()


def prescrits(verrou: dict, extras_du_projet: Iterable[str] = ()) -> dict[str, str]:
    """{nom canonique : version} que `uv sync --frozen [--extra …]` installerait pour
    CET interpréteur — la fermeture depuis la racine, marqueurs évalués ici."""
    paquets = verrou.get("package")
    if not isinstance(paquets, list) or not paquets:
        raise VerrouIllisible("aucune entrée [[package]] dans le verrou")
    par_nom: dict[str, list[dict]] = {}
    for p in paquets:
        par_nom.setdefault(nom_canonique(p["name"]), []).append(p)

    def cible(dep: dict) -> dict:
        candidats = par_nom.get(nom_canonique(dep["name"]), [])
        if "version" in dep:
            candidats = [c for c in candidats if c.get("version") == dep["version"]]
        if "source" in dep:
            candidats = [c for c in candidats if c.get("source") == dep["source"]]
        if len(candidats) != 1:
            raise VerrouIllisible(
                f"référence à `{dep['name']}` : {len(candidats)} paquet(s) du verrou "
                "y répondent, il en faut un")
        return candidats[0]

    racine = _racine(paquets)
    out: dict[str, str] = {}
    vus: set[tuple[str, str, str]] = set()
    pile: list[tuple[dict, frozenset]] = [(racine, frozenset(extras_du_projet))]
    while pile:
        paquet, extras = pile.pop()
        nom, version = nom_canonique(paquet["name"]), paquet.get("version", "")
        if out.setdefault(nom, version) != version:
            raise VerrouIllisible(
                f"`{nom}` prescrit en deux versions ({out[nom]} et {version}) pour le "
                "même interpréteur")
        deps = list(paquet.get("dependencies") or [])
        optionnelles = paquet.get("optional-dependencies") or {}
        for extra in extras:
            deps += list(optionnelles.get(extra) or [])
        for dep in deps:
            if not _vrai(dep.get("marker")):
                continue
            suivant = cible(dep)
            demandes = frozenset(dep.get("extra") or ())
            cle = (nom_canonique(suivant["name"]), suivant.get("version", ""),
                   ",".join(sorted(demandes)))
            if cle not in vus:
                vus.add(cle)
                pile.append((suivant, demandes))
    return out


def commits_du_verrou(verrou: dict) -> dict[str, str]:
    """{nom canonique : commit} des paquets que le verrou prend dans git — le commit
    résolu est le fragment de l'URL (`…?rev=v1.2.3#<commit>`)."""
    out = {}
    for p in verrou.get("package") or []:
        url = (p.get("source") or {}).get("git")
        if url and "#" in url:
            out[nom_canonique(p["name"])] = url.rsplit("#", 1)[1]
    return out


def extras_du_projet(verrou: dict) -> list[str]:
    return sorted((_racine(verrou.get("package") or []).get("optional-dependencies")
                   or {}).keys())


def ecarts(installe: dict[str, set[str]], prescrit: dict[str, str],
           commits_installes: Optional[dict[str, set[str]]] = None,
           commits_verrou: Optional[dict[str, str]] = None) -> list[str]:
    """Chaque différence, nommée. Vide = conforme."""
    out = []
    commits_installes, commits_verrou = commits_installes or {}, commits_verrou or {}
    for nom in sorted(set(installe) | set(prescrit)):
        versions, attendue = installe.get(nom), prescrit.get(nom)
        if versions is None:
            out.append(f"{nom} : manquant (verrou {attendue})")
        elif attendue is None:
            if nom in AMORCAGE:
                continue
            out.append(f"{nom}=={','.join(sorted(versions))} : hors verrou")
        elif len(versions) != 1 or not _meme_version(next(iter(versions)), attendue):
            out.append(f"{nom} : {','.join(sorted(versions))} installé, "
                       f"{attendue} au verrou")
        elif nom in commits_verrou and commits_installes.get(nom) != {commits_verrou[nom]}:
            vus = ",".join(sorted(commits_installes.get(nom) or {"aucun commit git"}))
            out.append(f"{nom} : commit {vus} installé, {commits_verrou[nom]} au verrou")
    return out


def _jeux(verrou: dict) -> list[frozenset]:
    """Les jeux d'extras du projet, du plus petit (le service) au plus grand."""
    extras = extras_du_projet(verrou)
    return [frozenset(c) for n in range(len(extras) + 1)
            for c in itertools.combinations(extras, n)]


def amorcage_prescrit(verrou: Optional[dict]) -> frozenset[str]:
    """Les paquets d'amorçage que le verrou prescrit pour cet interpréteur, pour l'un
    de ses jeux d'extras. Sans verrou : aucun."""
    if verrou is None:
        return frozenset()
    return frozenset(nom for jeu in _jeux(verrou) for nom in prescrits(verrou, jeu)
                     if nom in AMORCAGE)


def conformite(installe: dict[str, set[str]], verrou: dict,
               commits_installes: Optional[dict[str, set[str]]] = None,
               ) -> tuple[bool, list[str]]:
    """(conforme, écarts). Conforme si l'installé égale la fermeture du verrou pour
    L'UN des jeux d'extras du projet ; sinon, les écarts rendus sont ceux du jeu SANS
    extra — celui que le service installe."""
    premier: Optional[list[str]] = None
    for jeu in _jeux(verrou):
        e = ecarts(installe, prescrits(verrou, jeu), commits_installes,
                   commits_du_verrou(verrou))
        if not e:
            return True, []
        if premier is None:
            premier = e
    return False, premier or []


# ── l'état servi, calculé une fois ───────────────────────────────────────────

def etat(racine: Path) -> dict:
    """`{deps_sha, lock_sha, deps_conformes}` pour l'arbre `racine`. Non mémoïsé :
    c'est `version.instantane()` qui fige, une fois par processus."""
    distributions = list(metadata.distributions())
    installe = installees(distributions)
    chemin = racine / FICHIER_VERROU
    try:
        brut = chemin.read_bytes()
    except FileNotFoundError:
        log.warning("dépendances : aucun %s dans %s — conformité non établie",
                    FICHIER_VERROU, racine)
        return {"deps_sha": empreinte(installe, amorcage_prescrit(None)), "lock_sha": None,
                "deps_conformes": False}
    try:
        verrou = tomllib.loads(brut.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise VerrouIllisible(f"{chemin} : {e}") from e
    conforme, liste = conformite(installe, verrou, commits_vcs(distributions))
    if conforme:
        log.info("dépendances conformes au verrou (%d paquets)", len(installe))
    else:
        log.warning("dépendances NON conformes au verrou : %d écart(s) — %s%s",
                    len(liste), " ; ".join(liste[:_ECARTS_AU_JOURNAL]),
                    " ; …" if len(liste) > _ECARTS_AU_JOURNAL else "")
    return {"deps_sha": empreinte(installe, amorcage_prescrit(verrou)),
            "lock_sha": hashlib.sha256(brut).hexdigest(),
            "deps_conformes": conforme}


@lru_cache(maxsize=1)
def etat_au_demarrage(racine: Path) -> dict:
    """`etat()` figé par processus et par arbre — le parcours des distributions et du
    verrou ne se refait pas à chaque requête, ni à chaque purge du cache de version."""
    return etat(racine)

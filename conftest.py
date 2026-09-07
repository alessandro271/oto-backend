"""Le conftest du CONTRÔLEUR : chemin d'import, parallélisme, base partagée, bannière.

Pourquoi un second conftest, à la racine, alors que tout vivait dans `tests/` ?
**Parce que sous `pytest-xdist` le contrôleur ne collecte rien.** `tests/conftest.py`
n'est chargé que là où on collecte — donc uniquement dans les workers. Tout ce qui doit
se produire UNE fois pour la session entière (prendre le conteneur PostgreSQL, prendre
le jeton de concurrence, écrire la bannière du pin oto-core) n'a donc plus sa place
là-bas : un worker sur quatre ne parle pour personne, et un terminal de worker n'est lu
par personne. Ce fichier-ci, lui, est un conftest INITIAL : il est chargé dans le
contrôleur comme dans chaque worker, et en série comme en parallèle.

Trois choses, dans l'ordre où elles mordent :

1. **La racine du dépôt sur le `sys.path`.** `tests/test_l7_inversion.py` fait
   `from scripts import seed_everyone_edges` : dix cas étaient VERTS sous
   `python -m pytest` (qui met le dossier courant en tête du chemin) et ROUGES sous
   `pytest` (qui ne le fait pas) — le même dépôt, le même commit, deux verdicts selon
   le lanceur. Ce n'est pas au test de deviner comment on l'a lancé : la racine est
   posée ici, à partir de `__file__`, donc indépendamment du dossier courant.

2. **Le parallélisme, par défaut sur la suite COMPLÈTE seulement.** 11 223 cas en série
   coûtent 8-9 min ; à quatre workers, ~3 min. Mais lancer quatre workers pour vingt cas
   ciblés coûte plus cher que de les jouer en série, et — surtout — ferait prendre un
   conteneur et une place de jeton à une exécution qui n'ouvre aucune base, ce que
   `_jeton_de_suite` évite délibérément. D'où la règle : parallèle quand on ne nomme
   aucune cible (`pytest -q`, ce que fait la CI), série dès qu'on en nomme une.
   `-n N` explicite gagne toujours. `OTO_TEST_PARALLELE=0` rebascule tout en série —
   c'est la sortie, pour qu'un doute n'oblige personne à défaire ce fichier.

3. **Un seul PostgreSQL pour tous les workers.** La fixture `pg_box` est session-scopée :
   quatre workers = quatre sessions = quatre conteneurs, et quatre places demandées sur
   les **deux** que compte `_jeton_de_suite` — donc 900 s d'attente puis un délai expiré
   qui n'a rien à voir avec ce qu'on teste. Ici, le contrôleur NOMME le conteneur et
   possède sa sortie ; le premier worker qui demande vraiment une base la démarre sous
   ce nom et la publie aux autres (`tests/_pg_partage.py`). Il possède sans démarrer,
   parce qu'il ne collecte pas et ne peut donc pas savoir si une base sera ouverte —
   et qu'une exécution qui n'en ouvre aucune ne doit prendre aucune place.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

RACINE = Path(__file__).resolve().parent

# `tests/` porte les modules d'appui (`_oto_core_pin`, `_pg_hygiene`,
# `_jeton_de_suite`) ; la racine porte `scripts/`. Les deux, tout de suite : ce
# fichier les importe lui-même juste en dessous.
for _chemin in (str(RACINE / "tests"), str(RACINE)):
    if _chemin not in sys.path:
        sys.path.insert(0, _chemin)

from _oto_core_pin import (MARQUEUR, categorie_non_concluante,     # noqa: E402
                           ecart, lignes_de_banniere)
from _pg_hygiene import Guard, sweep_orphans                      # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Parallélisme
# --------------------------------------------------------------------------- #

#: Workers par défaut sur la suite complète. **Calibré, pas choisi** : deux suites
#: concurrentes suffisent à fabriquer de faux échecs sur ce poste (07/09/2026), donc
#: le parallélisme peut reproduire la même contention À L'INTÉRIEUR d'une suite. Le
#: nombre retenu est celui pour lequel deux passes identiques rendent le MÊME verdict.
WORKERS_DEFAUT = int(os.environ.get("OTO_TEST_WORKERS", "4"))

#: La sortie. `OTO_TEST_PARALLELE=0` ⟹ série, quoi qu'il arrive ; un entier ⟹ ce
#: nombre de workers, même sur une exécution ciblée.
VAR_PARALLELE = "OTO_TEST_PARALLELE"


def _cible_la_suite_entiere(config: pytest.Config) -> bool:
    """Vrai quand l'appelant n'a nommé NI fichier, NI dossier, NI filtre — c'est-à-dire
    quand il demande les onze mille cas. `pytest -q` sans argument atterrit ici avec
    `config.args == [racine]`."""
    if getattr(config.option, "keyword", "") or getattr(config.option, "markexpr", ""):
        return False
    cibles = {Path(str(a).split("::")[0]).resolve() for a in (config.args or [])}
    return bool(cibles) and cibles <= {RACINE, RACINE / "tests"}


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Pose `-n` AVANT que xdist ne lise son option (son propre `pytest_cmdline_main`
    traduit `numprocesses` en `dist`/`tx`, et il passe après celui d'un conftest)."""
    option = config.option
    if getattr(option, "numprocesses", None) is None:
        reglage = os.environ.get(VAR_PARALLELE, "").strip()
        if reglage == "0":
            return                               # la sortie
        if reglage.isdigit():
            option.numprocesses = int(reglage)
        elif not reglage and _cible_la_suite_entiere(config):
            option.numprocesses = WORKERS_DEFAUT
        else:
            return
    # Le MODE de distribution, lui, n'est pas posé ici : il vit dans `addopts`
    # (pyproject), seul endroit qu'un worker relit — il ne reçoit du contrôleur qu'un
    # dictionnaire d'options vide et la ligne de commande d'origine. Un `option.dist`
    # posé ici ne vaudrait que pour le contrôleur, et le groupage ne mordrait jamais.


# --------------------------------------------------------------------------- #
# 2. Le PostgreSQL partagé par tous les workers
# --------------------------------------------------------------------------- #

class Partage(NamedTuple):
    dossier: str                # là où les workers s'élisent et se passent le DSN
    nom: str                    # le conteneur, NOMMÉ à l'avance et possédé ici
    guard: Guard


def _preparer_le_partage(config: pytest.Config) -> Partage:
    """Nommer le conteneur et POSER sa garde — sans rien démarrer.

    Le contrôleur ne collecte pas : il ne sait pas si la session ouvrira une base. Il
    n'en ouvre donc aucune. Ce qu'il fait, et que personne d'autre ne peut faire, c'est
    posséder la sortie : `Guard` est armé sur un nom qui n'existe pas encore (un
    `docker rm` sur un conteneur absent ne coûte rien), et il retirera ce qui aura été
    démarré sous ce nom, quel que soit le worker qui l'aura démarré.
    """
    dossier = tempfile.mkdtemp(prefix="oto-pg-partage-")
    nom = f"oto-test-pg-{uuid.uuid4().hex[:8]}"
    guard = Guard(nom)
    guard.install()
    return Partage(dossier, nom, guard)


def pytest_configure_node(node) -> None:
    """Hook xdist, appelé UNIQUEMENT dans le contrôleur, une fois par worker."""
    config = node.config
    if config.getoption("collectonly", False):
        return                                   # une collecte n'ouvre aucune base
    partage = getattr(config, "_oto_partage_pg", None)
    if partage is None:
        partage = _preparer_le_partage(config)
        config._oto_partage_pg = partage         # type: ignore[attr-defined]
    node.workerinput["oto_pg_dossier"] = partage.dossier
    node.workerinput["oto_pg_nom"] = partage.nom


def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    """Le contrôleur retire le conteneur — il finit après tous ses workers, alors
    qu'un worker peut sortir pendant qu'un autre écrit encore."""
    partage = getattr(session.config, "_oto_partage_pg", None)
    if partage is None:
        return
    partage.guard.remove()
    partage.guard.uninstall()
    shutil.rmtree(partage.dossier, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. La bannière du pin oto-core, et le balai des conteneurs orphelins
# --------------------------------------------------------------------------- #
#
# Ces deux-là étaient dans `tests/conftest.py`, donc invisibles sous xdist (aucun
# terminal de worker n'est lu, et le balai s'y jouerait quatre fois). Ils vivent
# désormais là où il y a exactement un exemplaire et un terminal : ici.


@lru_cache(maxsize=1)
def _ecart_de_session():
    """Mesuré une fois par process. `.cache_clear()` pour les tests du garde-fou."""
    return ecart()


def _ecrire_banniere(reporter, skips: int = 0) -> None:
    e = _ecart_de_session()
    if e is None or reporter is None:
        return
    reporter.write_sep("=", "PIN oto-core", red=True, bold=True)
    for ligne in lignes_de_banniere(e, skips=skips):
        reporter.write_line(ligne, red=True, bold=ligne.startswith("oto-core"))


def pytest_sessionstart(session: pytest.Session) -> None:
    """La bannière AVANT le run (ne pas attendre la fin pour apprendre qu'on mesurait
    le mauvais oto-core), et le balai (#640) d'un conteneur qu'une session tuée a
    laissé. Rien de tout ça dans un worker : il n'a pas de terminal lu, et le balai
    n'a de sens qu'une fois."""
    if hasattr(session.config, "workerinput"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    _ecrire_banniere(reporter)
    for ligne in sweep_orphans(time.time()):
        if reporter is not None:
            reporter.write_line(ligne)
        else:
            print(ligne)


def pytest_report_teststatus(report, config: pytest.Config):
    """#790 — renommer la CATÉGORIE sous laquelle pytest compte les skips du pin, pour
    que le résumé final (la ligne qui survit à `| grep passed`) les nomme. Sous xdist
    les rapports remontent au contrôleur avec leurs `keywords` : ce hook, ici, voit
    donc les mêmes skips qu'en série."""
    if report.when != "setup" or not report.skipped:
        return None
    if MARQUEUR not in report.keywords:
        return None
    e = _ecart_de_session()
    if e is None:
        return None
    return categorie_non_concluante(e), "s", "NON CONCLUANT"


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """La MÊME bannière en fin de run — et c'est celle-ci qui compte : elle atterrit
    contre les `FAILED`, au moment précis où on se demande à qui sont ces rouges.

    Le nombre de non-concluants se LIT dans les stats du reporter plutôt que dans une
    variable posée à la collecte : sous xdist la collecte se fait ailleurs, et un
    compteur qui traverserait mal rendrait un zéro rassurant et faux."""
    e = _ecart_de_session()
    if e is None:
        return
    skips = len(terminalreporter.stats.get(categorie_non_concluante(e), []))
    _ecrire_banniere(terminalreporter, skips=skips)

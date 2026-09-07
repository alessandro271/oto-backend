"""Fixtures partagées.

`pg_dsn` — un PostgreSQL RÉEL, pour les rares tests qui n'ont de valeur que là :
une contrainte (la PK que viole un renommage naïf, #295) ou un opérateur JSONB
(`data - key`, qui efface là où `null` conserve, #296) ne s'exerce pas contre un
stub. Le reste de la suite reste sans base — la convention du repo est de tester
la logique pure et les gardes par stub, le chemin SQL étant vérifié au déploiement.

Source, dans l'ordre : le DSN que le contrôleur a poussé (sous xdist), sinon
`OTO_TEST_PG_DSN`, sinon un conteneur jetable si `docker` répond, sinon `skip`.
Un seul conteneur pour toute la suite, workers compris.

Le conteneur ne doit rien laisser derrière lui (#640, `_pg_hygiene.py`) : il est
étiqueté et daté, son `PGDATA` est un tmpfs (aucun volume), sa sortie est couverte
par `atexit` + SIGTERM/SIGINT en plus du finalizer, et chaque session commence par
balayer ce qu'une session tuée a laissé (`pytest_sessionstart`).

Ce fichier porte encore la MARQUE du pin oto-core — les tests qui n'ont de sens que
face au tag épinglé sont passés (non concluants) ici quand le venv est en retard.
Ce qui n'y est plus : la bannière, le balai des conteneurs orphelins et le renommage
de la catégorie de skip. Ils vivent dans le `conftest.py` de la RACINE, parce que
sous `pytest-xdist` le contrôleur ne collecte pas et ne charge donc jamais ce
fichier-ci : une bannière écrite depuis un worker n'est lue par personne.
"""
from __future__ import annotations

import os
import socket
import uuid
from functools import lru_cache
from typing import Iterator, NamedTuple, Optional

import pytest

from _oto_core_pin import MARQUEUR, ecart, skips_autorises
import _jeton_de_suite as jeton
import _pg_partage as partage


# --------------------------------------------------------------------------- #
# Garde-fou réseau : aucune connexion sortante réelle dans la suite
# --------------------------------------------------------------------------- #
#
# Mesure du 05/09/2026 (serper flaky, otomata-tech/oto#69) : sockets sortants
# bloqués pour toute la suite (10881 tests), UN SEUL a réellement dialé —
# `test_une_url_ordinaire_n_est_PAS_refusee` visait `serper._client`, qui
# n'existe pas au niveau module (fermeture locale de `register()`) ; le
# monkeypatch posait un attribut mort, `_Faux` n'était jamais exercé, et le
# `except Exception: pass` du test avalait l'appel réseau réel qui suivait
# (vers `exemple.invalid`, RFC 2606, pourtant résolu vers une IP live).
#
# Un test qui ouvre une connexion réelle est non déterministe PAR
# CONSTRUCTION : son issue dépend de l'horloge, du réseau et des conditions du
# moment — et un rouge intermittent se fait accuser au dernier commit poussé,
# jamais à sa vraie cause. Fermé structurellement plutôt que corrigé au cas
# par cas : le loopback (tests DB réels, `pg_dsn`) reste libre, tout le reste
# est bloqué par défaut. Le besoin légitime existe : il se déclare, avec sa
# raison, à l'endroit où il se présente — jamais une exemption muette.
_MARQUEUR_RESEAU = "reseau_reel"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_reseau_autorise_pour: list[str] = []  # pile de raisons — le test courant, s'il a le droit


def _connexion_gardee(self: socket.socket, address):
    host = address[0] if isinstance(address, tuple) else address
    if host in _LOOPBACK or _reseau_autorise_pour:
        return _SOCKET_CONNECT_ORIGINAL(self, address)
    raise AssertionError(
        f"connexion sortante bloquée vers {address!r} — cette suite n'ouvre "
        f"aucune connexion réseau réelle (otomata-tech/oto#69, 05/09/2026). "
        f"Un test qui en a légitimement besoin le déclare avec "
        f"@pytest.mark.{_MARQUEUR_RESEAU}(\"pourquoi un stub ne suffit pas ici\").")


_SOCKET_CONNECT_ORIGINAL = socket.socket.connect
socket.socket.connect = _connexion_gardee


@pytest.fixture(autouse=True)
def _garde_reseau_sortant(request: pytest.FixtureRequest) -> Iterator[None]:
    marker = request.node.get_closest_marker(_MARQUEUR_RESEAU)
    if marker is None:
        yield
        return
    if not marker.args or not str(marker.args[0]).strip():
        raise TypeError(
            f"@pytest.mark.{_MARQUEUR_RESEAU} exige une raison : "
            f'@pytest.mark.{_MARQUEUR_RESEAU}("pourquoi un stub ne suffit pas ici")')
    _reseau_autorise_pour.append(marker.args[0])
    try:
        yield
    finally:
        _reseau_autorise_pour.pop()


# --------------------------------------------------------------------------- #
# Pin oto-core : le venv exécute-t-il ce que le tronc épingle ?
# --------------------------------------------------------------------------- #
#
# Sept sessions ont enquêté sur le même faux rouge le 01/09/2026, dont une qui a
# conclu « le tronc est rouge, plus aucune PR ne peut entrer » pendant que la CI
# était verte. La doc décrivait déjà le piège — donc ce n'est pas la doc qui
# manquait, c'est le forçage. Le voici.


@lru_cache(maxsize=1)
def _ecart_de_session():
    """Mesuré une fois par run. `.cache_clear()` pour les tests du garde-fou."""
    return ecart()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{MARQUEUR}: ce test n'a de SENS que face à l'oto-core épinglé — il est "
        "passé (non concluant) en local quand le venv est en retard sur le pin, "
        "et reste mordant en CI.")
    config.addinivalue_line(
        "markers",
        f"{_MARQUEUR_RESEAU}(raison): autorise CE test à ouvrir une connexion "
        "réseau sortante réelle (non-loopback) — la raison est OBLIGATOIRE, "
        "elle documente pourquoi un stub ne suffit pas ici.")


#: Les fichiers qu'on ne laisse jamais tourner en même temps les uns que les autres.
#:
#: ⚠️ **Le couple qui compte est un vrai conflit MESURÉ, pas une précaution.**
#: `test_pg_fixture_hygiene` fabrique un conteneur délibérément VIEUX (`_factice(now -
#: 3*3600)`, étiqueté `oto-test=1`) puis vérifie que le balai le retire, ou qu'un balai
#: ne retire rien. `test_pin_oto_core_banniere`, lui, lance dix-huit pytest ENFANTS ;
#: chacun charge le `conftest.py` de la racine, dont `pytest_sessionstart` appelle
#: `sweep_orphans()` — qui énumère par ÉTIQUETTE tous les conteneurs de la machine et
#: retire ceux de plus de deux heures. L'enfant de l'un supprime donc le factice de
#: l'autre, et l'assertion tombe sur un conteneur déjà parti. Ces deux fichiers écrivent
#: dans le MÊME espace de noms global — celui de l'étiquette docker — et rien d'autre
#: dans la suite ne les sépare.
#:
#: `test_empreinte_servie` est là par PRÉCAUTION, pas sur preuve : il n'observe aucun
#: conteneur et n'écrit pas dans l'arbre, donc aucun worker ne peut le faire mentir. Ce
#: qu'il a, c'est le profil (sept sous-processus, dont deux qui relisent l'arbre git) qui
#: fabrique des faux échecs sous contention. Neuf cas : le garder ne coûte presque rien,
#: et l'en retirer demanderait une mesure qu'on n'a pas faite.
FICHIERS_EN_SERIE = ("test_pg_fixture_hygiene.py",
                     "test_pin_oto_core_banniere.py",
                     "test_empreinte_servie.py")
GROUPE_SERIE = "serie-lourde"


def _grouper_pour_xdist(config: pytest.Config, items) -> None:
    """Un groupe par FICHIER, plus un groupe commun aux fichiers à état global.

    Le groupe par fichier redonne à `--dist loadgroup` le comportement de
    `loadfile` : tout un fichier part au même worker. Sans ça, les 100 fixtures de
    module qui créent chacune leur base (comptées le 08/09/2026 : 181 fixtures de
    module en tout, 107 fichiers portant un `CREATE DATABASE`) seraient rejouées dans
    chaque worker recevant un morceau du fichier — soit exactement la charge de
    créations et destructions concurrentes qui sature le serveur. Le groupe commun,
    lui, `loadfile` ne sait pas l'exprimer : c'est la seule raison de préférer
    `loadgroup`.
    """
    # ⚠️ PAS `config.getoption("dist")` : xdist remet `dist` à « no » dans chaque
    # worker (`remote.setup_config`) pour qu'il ne redistribue pas à son tour, et
    # c'est justement dans le worker que la collecte a lieu. Le drapeau qui survit,
    # et que xdist lit lui-même, c'est `option.loadgroup`.
    if not getattr(config.option, "loadgroup", False):
        return                                 # en série, un marqueur de plus ne sert
    for item in items:
        fichier = getattr(item, "path", None)
        nom = fichier.name if fichier is not None else ""
        groupe = (GROUPE_SERIE if nom in FICHIERS_EN_SERIE
                  else item.nodeid.split("::")[0])
        item.add_marker(pytest.mark.xdist_group(groupe))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    """⚠️ `tryfirst` : dans un worker, xdist enregistre son `WorkerInteractor` APRÈS
    les conftests initiaux, donc il est appelé AVANT eux — et c'est lui qui recopie le
    groupe dans le nodeid. Sans `tryfirst`, les marqueurs posés ci-dessous arrivent
    une fois la lecture faite : tout est marqué, rien n'est groupé (mesuré le
    08/09/2026 — aucun nodeid ne portait son suffixe `@groupe`).

    Un rouge qui ne prouve rien vaut moins qu'un test explicitement non
    concluant — mais SEULEMENT en local : en CI la garde version-skew doit mordre,
    c'est tout son objet (cf. `skips_autorises`)."""
    _grouper_pour_xdist(config, items)
    config.stash_oto_core_skips = 0            # type: ignore[attr-defined]
    e = _ecart_de_session()
    if e is None or not skips_autorises():
        return
    marque = pytest.mark.skip(
        reason=f"oto-core installé ({e.installe or 'aucun'}) ≠ épinglé "
               f"({e.epingle}) — non concluant dans cet environnement")
    vises = [item for item in items if item.get_closest_marker(MARQUEUR)]
    for item in vises:
        item.add_marker(marque)
    config.stash_oto_core_skips = len(vises)   # type: ignore[attr-defined]


class PgBox(NamedTuple):
    dsn: str
    container: Optional[str]   # None quand la base vient d'`OTO_TEST_PG_DSN`


@pytest.fixture(scope="session")
def pg_box(request: pytest.FixtureRequest) -> Iterator[PgBox]:
    # Sous xdist : le conteneur est NOMMÉ et possédé par le contrôleur, et le premier
    # worker qui passe ici le démarre pour tout le monde — un seul conteneur, une seule
    # place de jeton, et rien du tout si personne ne demande de base. Quatre workers qui
    # créeraient chacun le leur, ce serait quatre conteneurs et quatre places demandées
    # sur les deux qui existent. Détail et élection : `_pg_partage.py`.
    entree = getattr(request.config, "workerinput", {})
    if "oto_pg_dossier" in entree:
        dsn, conteneur = partage.base_partagee(entree["oto_pg_dossier"],
                                               entree["oto_pg_nom"])
        if not dsn:
            pytest.skip("aucun PostgreSQL joignable (ni OTO_TEST_PG_DSN, ni docker)")
        yield PgBox(dsn, conteneur)   # le contrôleur le retirera, pas ce worker
        return
    # Le JETON d'abord, avant de toucher au serveur : au-delà de deux suites en
    # parallèle sur ce poste, elles se fabriquent mutuellement de faux échecs (neuf
    # simultanées le 07/09/2026). Pris ICI et pas au démarrage de la session — une
    # exécution ciblée qui n'ouvre aucune base ne consomme aucune place, sinon la
    # garde punirait le geste qu'elle veut encourager. Détail : `_jeton_de_suite`.
    jeton.prendre()
    nom = f"oto-test-pg-{uuid.uuid4().hex[:8]}"
    dsn, guard = partage.demarrer(nom)
    try:
        if not dsn:
            pytest.skip("aucun PostgreSQL joignable, ou pas devenu prêt "
                        "(ni OTO_TEST_PG_DSN, ni docker)")
        yield PgBox(dsn, nom if guard is not None else None)
    finally:
        if guard is not None:
            guard.remove()
            guard.uninstall()


@pytest.fixture(scope="session")
def pg_dsn(pg_box: PgBox) -> str:
    return pg_box.dsn

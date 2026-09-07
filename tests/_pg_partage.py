"""Un seul PostgreSQL pour toute la session, workers compris (08/09/2026).

La fixture `pg_box` est session-scopée : en série, un conteneur pour toute la suite.
Sous `pytest-xdist`, « une session » devient N sessions — une par worker — et la même
fixture lancerait N conteneurs, en demandant N places sur les **deux** que compte
`_jeton_de_suite`. Les workers en trop attendraient 900 s, puis la suite tomberait sur
un délai expiré qui n'a rien à voir avec ce qu'on teste.

**Qui prend quoi.** Le contrôleur ne collecte rien sous xdist : il ne peut pas savoir
si la session ouvrira une base. Il se contente donc de NOMMER le conteneur à l'avance
et de le POSSÉDER (c'est lui qui le retirera — il finit après tous ses workers, alors
qu'un worker peut sortir pendant qu'un autre écrit encore). Le premier worker qui
demande réellement une base est celui qui la prend : c'est cette élection qui préserve
la propriété que `_jeton_de_suite` protège — **une exécution qui n'ouvre aucune base ne
consomme aucune place**. Sans elle, `-n 4` sur vingt cas ciblés ferait la queue derrière
deux suites complètes pour un conteneur dont personne ne se servirait.

L'élection tient dans un `flock` : le premier arrivé le garde pendant qu'il démarre le
serveur (quelques secondes), les autres bloquent dessus puis lisent le DSN qu'il a
écrit. Écriture atomique (`os.replace`) — un worker qui mourrait au milieu laisserait
un fichier absent, pas un DSN tronqué.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional, Tuple

from _pg_hygiene import Guard, docker_available, run_args


def attendre_le_serveur(dsn: str, secondes: int = 60) -> bool:
    """L'attente se fait avec L'INSTRUMENT DU TEST — une vraie connexion depuis l'hôte.

    `pg_isready` dans le conteneur répond OK pendant la phase d'INIT de l'image
    postgres (serveur temporaire, socket locale), puis le serveur redémarre : les
    premiers tests tombaient alors sur « server closed the connection unexpectedly ».
    Un sondage qui n'emprunte pas le chemin du test ne prouve pas qu'il est prêt.
    """
    try:
        import psycopg
    except ImportError:                          # pragma: no cover — venv sans psycopg
        return False
    limite = time.time() + secondes
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            if time.time() > limite:
                return False
            time.sleep(1)


def demarrer(nom: str, *, garde: bool = True) -> Tuple[str, Optional[Guard]]:
    """Le PostgreSQL de la session : `OTO_TEST_PG_DSN`, sinon un conteneur jetable,
    sinon `("", None)` — l'appelant skippe.

    ⚠️ `garde=False` dans un worker xdist : le `Guard` s'accroche à `atexit`, et un
    worker qui sort en premier retirerait le conteneur sous les pieds de ceux qui
    tournent encore. Sous xdist, c'est le contrôleur qui possède le conteneur.
    """
    dsn = os.environ.get("OTO_TEST_PG_DSN")
    if dsn:
        return dsn, None
    if not docker_available():
        return "", None
    try:
        subprocess.run(run_args(nom), capture_output=True, check=True)
    except Exception:                            # pragma: no cover — démon capricieux
        return "", None
    guard = Guard(nom) if garde else None
    if guard is not None:
        guard.install()
    try:
        port = subprocess.run(["docker", "port", nom, "5432/tcp"], capture_output=True,
                              text=True, check=True).stdout.strip().rsplit(":", 1)[1]
    except Exception:                            # pragma: no cover
        return "", guard
    dsn = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
    if not attendre_le_serveur(dsn):
        return "", guard
    return dsn, guard


def base_partagee(dossier: str, nom: str) -> Tuple[str, Optional[str]]:
    """L'élection, côté worker. Rend `(dsn, conteneur)` — `("", None)` si aucune base
    n'est joignable, auquel cas TOUS les workers skippent, pas seulement l'élu.

    Le nom du conteneur est rendu à chaque worker, pas seulement à celui qui l'a
    démarré : `test_pg_fixture_hygiene` inspecte ce conteneur (labels, absence de
    volume, #640) et ce contrôle deviendrait un skip silencieux sous xdist s'il ne
    voyait qu'un DSN nu.
    """
    try:
        import fcntl
    except ImportError:                          # pragma: no cover — pas de flock ici
        dsn, _ = demarrer(nom, garde=False)
        return dsn, (nom if dsn and not os.environ.get("OTO_TEST_PG_DSN") else None)
    fichier = os.path.join(dossier, "etat")
    with open(os.path.join(dossier, "verrou"), "w") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        if os.path.exists(fichier):
            with open(fichier) as f:
                etat = json.load(f)
            return etat["dsn"], etat["conteneur"]
        # Le JETON, ici et pas au démarrage de la session : c'est le premier worker
        # qui demande VRAIMENT une base qui prend la place, une seule fois pour la
        # session entière. Détail : `_jeton_de_suite`.
        import _jeton_de_suite as jeton
        jeton.prendre()
        depuis_env = bool(os.environ.get("OTO_TEST_PG_DSN"))
        dsn, _ = demarrer(nom, garde=False)
        etat = {"dsn": dsn, "conteneur": None if (depuis_env or not dsn) else nom}
        temporaire = fichier + ".tmp"
        with open(temporaire, "w") as f:
            json.dump(etat, f)
        os.replace(temporaire, fichier)          # atomique : jamais un état tronqué
        return etat["dsn"], etat["conteneur"]

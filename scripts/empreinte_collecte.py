#!/usr/bin/env python3
"""Combien de mémoire coûte une collecte de la suite, sur LA MACHINE QUI EXÉCUTE.

POURQUOI CETTE MESURE EXISTE.

Le 07/09/2026 à 23:45 UTC, `937eb8f7` a posé `WORKERS_DEFAUT = 4`, un nombre FIXE,
calibré sur un poste à 28 cœurs. Le job `test` tourne sur un `ubuntu-latest` à 4 vCPU
et 16 Go : quatre workers y collectent chacun les 11 000+ cas et importent tout le
serveur en même temps. L'OOM killer a emporté `pytest` sept fois de suite (exit 137,
entre 108 s et 218 s, toujours PENDANT la collecte, avant même l'en-tête de xdist).
Le tronc est resté rouge six heures, la préproduction figée, la production bloquée
avec elle — `deploy.yml` portait alors le même job `test` en `needs`.

La leçon écrite dans le revert : « un nombre de workers se dérive de la machine qui
EXÉCUTE — sa mémoire disponible, pas seulement ses cœurs — jamais de celle qui
mesure. » Sauf que cette règle était inapplicable : **personne n'a jamais mesuré ce
que pèse une collecte.** Recherché dans les workflows, `docs/` et `scripts/` : aucune
trace. Le chiffre 4 n'était pas seulement mal calibré, il n'était adossé à rien —
et le cadre qui l'a tué est lui-même une DÉDUCTION depuis la mort, pas une mesure.

Ce script produit le chiffre qui manquait. Il ne règle rien, il ne décide rien : il
mesure et il publie. C'est ce qui rendra le parallélisme décidable au lieu de deviné.

CE QU'IL MESURE, ET POURQUOI DEUX FOIS.

1. L'import de `oto_mcp.server` seul. Importer ce module construit une instance MCP
   complète au niveau du module (ADR 0065) : c'est le gros de ce que tout process de
   test paiera, worker xdist compris.
2. La collecte complète (`pytest --collect-only`). C'est le pic réel observé lors de
   l'OOM — la mort tombait pendant la collecte, jamais pendant les tests.

L'écart entre les deux dit où va la mémoire : dans le serveur, ou dans les 814
fichiers de test et leurs fixtures.

CE QU'IL N'EST PAS.

Ce n'est pas un réglage. Le nombre de workers qu'il imprime est une CONSÉQUENCE de ce
qu'il vient de mesurer sur la machine où il tourne, publiée pour qu'on la lise — pas
une valeur à recopier ailleurs. Recopier un nombre d'une machine sur une autre est
exactement le geste qui a gelé le tronc.

Il ne sort JAMAIS en échec. Un instrument qui peut faire rougir un run devient un
risque au lieu d'un service — et depuis le 08/09 la garde de mise en production lit
la conclusion du run de préproduction : une mesure qui échoue bloquerait une release.
Le job qui l'appelle porte en plus `continue-on-error: true`. Ceinture et bretelles,
délibérément.
"""

from __future__ import annotations

import os
import resource  # noqa: F401 - utilisé par le lanceur intermédiaire
import subprocess
import sys
import time

# Marge laissée au système et au processus contrôleur. La mémoire « disponible » d'un
# runner n'est pas toute allouable : le noyau, le démon du runner et le contrôleur
# xdist vivent dedans. 20 % est prudent, et le but ici est de ne PAS reproduire un
# chiffre optimiste.
MARGE = 0.80


def _meminfo() -> dict:
    valeurs = {}
    try:
        with open("/proc/meminfo") as flux:
            for ligne in flux:
                cle, _, reste = ligne.partition(":")
                morceaux = reste.split()
                if morceaux:
                    valeurs[cle] = int(morceaux[0]) * 1024  # kB → octets
    except OSError:
        pass
    return valeurs


def _go(octets: float) -> str:
    return "{:.2f} Go".format(octets / (1024**3))


# ⚠️ CHAQUE MESURE SE FAIT DANS UN PROCESSUS NEUF, ET C'EST LE CŒUR DE L'INSTRUMENT.
#
# `RUSAGE_CHILDREN.ru_maxrss` n'est pas le coût du dernier fils : c'est le MAXIMUM
# atteint par tous les fils déjà moissonnés, et il ne redescend jamais. Mesurer deux
# commandes de suite depuis le même processus attribue donc à la seconde le pic de la
# première. Constaté ici même en étalonnant : après un fils qui alloue 300 Mo, un
# `python -c pass` était lui aussi rapporté à 0,31 Go — l'instrument fabriquait son
# signal, et il l'aurait fait dans le sens qui arrange (une collecte « chère »).
#
# Le remède : un lanceur intermédiaire jetable par mesure. Son `RUSAGE_CHILDREN` à lui
# ne connaît qu'un seul fils, donc le chiffre est isolé par construction.
_LANCEUR = (
    "import json,resource,subprocess,sys\n"
    "c=subprocess.run(json.loads(sys.argv[1]),"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(json.dumps({'code': c.returncode,"
    " 'rss': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))\n"
)


def _mesurer(intitule: str, commande: list) -> int:
    """Lance la commande dans un lanceur jetable et rend son pic de RSS, en octets."""
    import json

    depart = time.monotonic()
    try:
        acheve = subprocess.run(
            [sys.executable, "-c", _LANCEUR, json.dumps(commande)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        releve = json.loads(acheve.stdout.strip().splitlines()[-1])
        code = releve["code"]
        pic = releve["rss"] * 1024  # ru_maxrss est en kilo-octets sous Linux
    except Exception as erreur:  # binaire absent, délai dépassé, relevé illisible…
        print("  {} : NON MESURÉ ({})".format(intitule, erreur))
        return 0
    duree = time.monotonic() - depart

    if code != 0:
        # Le cas qui compte : un `pytest` TUÉ par l'OOM killer sort en 137. Le dire en
        # toutes lettres, parce que c'est précisément le symptôme qu'on instrumente.
        cause = " (SIGKILL — probablement l'OOM killer)" if code == 137 else ""
        print(
            "  {} : sortie {}{} après {:.0f} s — pic relevé {}".format(
                intitule, code, cause, duree, _go(pic)
            )
        )
        return pic

    print("  {} : {} en {:.0f} s".format(intitule, _go(pic), duree))
    return pic


def main() -> int:
    memoire = _meminfo()
    total = memoire.get("MemTotal", 0)
    disponible = memoire.get("MemAvailable", 0)
    coeurs = os.cpu_count() or 0

    print("=== Empreinte d'une collecte — mesurée SUR CETTE MACHINE ===")
    print(
        "Machine : {} cœur(s), {} de mémoire totale, {} disponible au démarrage".format(
            coeurs, _go(total), _go(disponible)
        )
    )
    print("Mesures (pic de RSS du processus fils) :")

    serveur = _mesurer(
        "import de oto_mcp.server",
        [sys.executable, "-c", "import oto_mcp.server"],
    )
    collecte = _mesurer(
        "collecte complète (pytest --collect-only)",
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
    )

    print()
    if not collecte or not disponible:
        print(
            "Rien à dériver : la collecte n'a pas pu être mesurée, ou /proc/meminfo est "
            "muet. Aucun nombre de workers ne doit être choisi sur cette base."
        )
        return 0

    if serveur:
        part = 100.0 * serveur / collecte
        print(
            "Le serveur pèse {} des {} de la collecte ({:.0f} %) : le reste vient des "
            "fichiers de test et de leurs fixtures.".format(
                _go(serveur), _go(collecte), part
            )
        )

    budget = disponible * MARGE
    # Un worker xdist collecte pour son propre compte : il paie le pic complet. Le
    # contrôleur, lui, ne collecte pas — mais il vit, d'où le « - 1 ».
    tiennent = int(budget // collecte)
    workers = max(1, tiennent - 1)
    print(
        "Sur {} disponibles, {} utilisables ({:.0f} %) : {} process de collecte y "
        "tiennent, soit {} worker(s) une fois le contrôleur déduit.".format(
            _go(disponible), _go(budget), MARGE * 100, tiennent, workers
        )
    )
    if coeurs and workers > coeurs:
        print(
            "Borné par les cœurs : {} au lieu de {} (au-delà, on paie la mémoire sans "
            "gagner de temps).".format(coeurs, workers)
        )
        workers = coeurs
    print()
    print(
        "→ NOMBRE DÉRIVÉ POUR CETTE MACHINE : {}. Ce n'est pas un réglage à recopier "
        "ailleurs — c'est ce que CETTE machine supporte, aujourd'hui, avec cet "
        "arbre.".format(workers)
    )
    if workers < 2:
        print(
            "  ⚠️ Moins de deux : sur cette machine, paralléliser la suite n'est pas "
            "possible sans réduire d'abord l'empreinte d'une collecte."
        )
    return 0


if __name__ == "__main__":
    # Jamais un code de sortie non nul : cet instrument ne doit pouvoir bloquer aucune
    # release (cf. l'en-tête — la garde de mise en production lit la conclusion du run).
    try:
        sys.exit(main())
    except Exception as erreur:  # pragma: no cover - filet de dernier recours
        print("Mesure impossible : {}".format(erreur))
        sys.exit(0)

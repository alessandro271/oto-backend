"""Le JETON qui borne combien de suites touchent PostgreSQL en même temps (07/09/2026).

Incident fondateur, mesuré le soir même : **neuf suites lancées en parallèle** sur le
poste partagé par sept sessions d'agents qui ne se voient pas les unes les autres —
charge 10,3, processeur à 68 °C, dix-huit processus. Le coût n'était pas thermique : on
avait déjà mesuré cette semaine que **deux** suites concurrentes suffisent à fabriquer
de FAUX ÉCHECS. À neuf, le verdict de chacune était sans valeur.

⚠️ **C'est ce mode de défaillance qui justifie une garde, pas la lenteur.** Une suite
lente coûte des minutes ; une suite qui rougit sans raison coûte l'enquête qui suit —
on instruit une contention en croyant instruire une régression, et on cherche dans son
propre code un défaut qui n'y est pas.

**Où ça casse réellement** — cherché, pas supposé : chaque suite crée sa PROPRE base
jetable, il n'y a donc aucun nom partagé à protéger. Ce qui sature, c'est le SERVEUR :
créations et destructions de bases concurrentes se disputent ses connexions et ses
verrous de catalogue. D'où un **jeton de concurrence**, jamais un verrou de base.

**Deux places autorisées** (tranché par Alexis le 07/09/2026, sur la mesure « deux
suffisent à casser » : deux, c'est ce qui reste utilisable en gardant du parallélisme).

⚠️ **Le jeton se prend là où la base est demandée, pas au démarrage de la session.**
Une exécution ciblée qui n'ouvre aucune base ne consomme aucune place — sans quoi la
garde punirait précisément le geste qu'elle veut encourager : jouer le sous-ensemble
qui couvre ce qu'on touche plutôt que les onze mille cas.

⚠️ **L'attente est BORNÉE et elle s'explique.** Une contention qui deviendrait un gel
silencieux serait le défaut qu'on traque, posé de nos propres mains dans le chemin de
tous. À l'expiration, on DIT combien de places existent, depuis combien de temps on
attend, et quoi faire — jamais un blocage muet.

En intégration continue, chaque job a son propre système de fichiers : les jetons y sont
locaux au conteneur, la garde ne sérialise donc rien. C'est voulu — elle vise le poste
partagé, et elle le reconnaît par construction plutôt que par un drapeau à régler.
"""
from __future__ import annotations

import os
import time
from typing import Optional

#: Places simultanées. Relevable par l'environnement pour une machine plus grosse.
PLACES = int(os.environ.get("OTO_TEST_PG_PLACES", "2"))

#: Borne d'attente. Une suite complète dure ~5 min : de quoi laisser passer quelques
#: tours de file sans jamais devenir un gel.
ATTENTE_MAX_S = int(os.environ.get("OTO_TEST_PG_ATTENTE_S", "900"))

_REPERTOIRE = "/tmp/oto-jetons-suite"

#: Gardé au niveau du module : le descripteur doit VIVRE aussi longtemps que la
#: session, sinon le ramasse-miettes relâche le verrou sans que personne le demande.
_TENU: list = []


def prendre(attendre_max_s: Optional[int] = None) -> Optional[int]:
    """Occupe une des places, ou lève en DISANT pourquoi. Rend le numéro de la place.

    Rend `None` sans rien bloquer si le système ne sait pas verrouiller — une garde
    de confort ne doit jamais empêcher la suite de tourner là où elle ne s'applique
    pas (Windows, système de fichiers en lecture seule, bac à sable).
    """
    try:
        import fcntl
    except ImportError:                       # pragma: no cover — pas de flock ici
        return None
    try:
        os.makedirs(_REPERTOIRE, exist_ok=True)
    except OSError:                           # pragma: no cover — /tmp non inscriptible
        return None

    limite = time.monotonic() + (ATTENTE_MAX_S if attendre_max_s is None
                                 else attendre_max_s)
    depart = time.monotonic()
    while True:
        for place in range(PLACES):
            try:
                fd = os.open(f"{_REPERTOIRE}/place-{place}", os.O_CREAT | os.O_RDWR, 0o666)
            except OSError:                   # pragma: no cover
                return None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)                  # occupée : on essaie la suivante
                continue
            _TENU.append(fd)                  # tenu jusqu'à la fin du processus
            return place
        if time.monotonic() >= limite:
            attendu = int(time.monotonic() - depart)
            raise TimeoutError(
                f"Les {PLACES} places de test PostgreSQL sont occupées depuis "
                f"{attendu} s.\n"
                "\n"
                "Ce n'est pas une panne : d'autres suites tournent sur ce poste, et "
                "au-delà de deux en parallèle elles se fabriquent mutuellement de faux "
                "échecs — le verdict de chacune cesse d'avoir un sens.\n"
                "\n"
                "Trois sorties, de la meilleure à la moins bonne :\n"
                f"  1. attendre que l'une finisse (~5 min pour une suite complète) ;\n"
                "  2. NOMMER les fichiers de bancs qui couvrent ce que tu touches, "
                "plutôt qu'un dossier. Un banc qui n'ouvre pas de vraie base ne prend "
                "aucune place — et c'est la plupart d'entre eux. Mesuré : "
                "`pytest tests/datastore/ -q` prend une place (quelques fichiers du "
                "dossier demandent la fixture `live`), alors que "
                "`pytest tests/datastore/test_datastore_pattern.py -q` n'en prend "
                "aucune. La règle est au FICHIER, pas au dossier ;\n"
                f"  3. sur une machine plus grosse, relever `OTO_TEST_PG_PLACES` "
                f"(actuellement {PLACES}) — mais la mesure qui a fixé ce nombre dit que "
                "deux suites concurrentes suffisent déjà à mentir.")
        time.sleep(1.0)

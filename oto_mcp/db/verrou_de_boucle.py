"""Une boucle de fond ne tourne que dans UN processus à la fois, tour par tour.

Pourquoi : deux processus du serveur branchés sur la même base — les deux couleurs du
bleu/vert pendant la vidange, ou plusieurs processus qui servent la même instance —
composent chacun les mêmes boucles. Une boucle qui réclame sa file sans la sortir de
l'état « à faire » agit alors deux fois vers l'extérieur : le scheduler d'emails
(`claim_due_scheduled_emails` incrémente `attempts` mais laisse la ligne `pending`)
envoyait le même message deux fois ; les alertes aux admins et sur Slack marquent APRÈS
l'envoi, donc deux passages simultanés écrivaient deux fois au client.

**Le verrou est un verrou consultatif PostgreSQL de SESSION, sur une connexion dédiée,
hors pool, en autocommit, tenu le temps d'UN tour** (`tour`). Chaque choix compte :

- de SESSION, pas de transaction (`pg_try_advisory_xact_lock`) : un tour dure le temps de
  ses appels réseau (un lot d'emails, une relance Mollie, un balayage Slack). Une
  transaction tenue ouverte tout ce temps serait coupée par
  `idle_in_transaction_session_timeout` (60 s) — et le verrou avec elle, en plein tour,
  sans que personne ne le voie. En autocommit, la connexion n'est jamais « en
  transaction » : rien ne la coupe ;
- HORS POOL : un verrou de session reste attaché à sa connexion ; rendue au pool, elle
  tiendrait la boucle pour tout le monde, à jamais. Ici la connexion n'appartient qu'au
  tour : `pg_advisory_unlock` dans un `finally`, puis la fermeture, qui rend le verrou
  quoi qu'il arrive — exception, `unlock` raté, processus tué (le serveur le relâche à la
  fin de la session) ;
- hors des places de `_connect_autocommit` (plafonnées à 2 par processus, pour le DDL à
  chaud) : un tour tient sa connexion plusieurs secondes, il ne doit pas faire attendre
  un `CREATE INDEX CONCURRENTLY` servi à un utilisateur. Coût : une connexion PostgreSQL
  de plus par boucle verrouillée, le temps de son tour ;
- `try` : le second processus n'attend pas, il SAUTE le tour. Le travail ne lui échappe
  pas : au tour suivant, celui qui tient la boucle l'aura fait, ou la boucle sera libre.

**Clé** : la forme à deux entiers de PostgreSQL, comme `billing_reservation` et
`runner_fleets` — le premier isole la FAMILLE (`_FAMILLE`, propre aux boucles), le second
est un hachage FIGÉ du nom de la boucle (`crc32`, stable d'un processus et d'une version
à l'autre, contrairement à `hash()`). Les verrous à un seul entier (`init_db`,
`hashtext(...)` du coffre et des guides) vivent dans un autre espace de clés : PostgreSQL
ne confond jamais une clé à un entier et une clé à deux.

**Base injoignable = erreur**, jamais un tour sauté en silence : la connexion lève, la
boucle le journalise comme tout tour raté.
"""
from __future__ import annotations

import logging
import zlib
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

import psycopg

from . import _hors_boucle
from ._conn import _connect_options, _database_url, _str_dict_row

log = logging.getLogger(__name__)

T = TypeVar("T")

# Famille de verrous propre aux boucles de fond (premier entier de la clé). Même
# convention que `_VERROU_CAMPAGNE` (0x0704_0C41) et `_VERROU_ECHEANCE` (0x0B11_0043).
_FAMILLE = 0x0704_0B0C   # « oto boucle »

# Les boucles dont CE processus a déjà dit qu'un autre les tient : on le dit en INFO au
# premier tour sauté, puis en DEBUG tant que ça dure (deux processus permanents sauteraient
# un tour par minute et par boucle). Un tour tenu remet le compteur à zéro.
_SAUTEES_DITES: set[str] = set()


def cle(nom: str) -> int:
    """Le second entier de la clé : un hachage figé du nom, en `int4` signé."""
    h = zlib.crc32(nom.encode("utf-8"))
    return h - (1 << 32) if h >= (1 << 31) else h


def _connexion() -> psycopg.Connection:
    """Une connexion dédiée, hors pool, en autocommit. Sans `statement_timeout` de
    processus : elle ne porte que deux requêtes instantanées, et un tour long est le cas
    nominal, pas un gel."""
    _hors_boucle.verifier()
    return psycopg.connect(_database_url(), options=_connect_options(borne_par_defaut=False),
                           row_factory=_str_dict_row, autocommit=True)


def _relacher(conn: psycopg.Connection, nom: str) -> None:
    """`pg_advisory_unlock` explicite. S'il échoue (connexion rompue en plein tour), on le
    DIT : la fermeture qui suit rend le verrou de toute façon, mais un verrou perdu en
    cours de tour veut dire qu'un autre processus a pu jouer le même tour en même temps."""
    try:
        rendu = conn.execute("SELECT pg_advisory_unlock(%s, %s) AS rendu",
                             (_FAMILLE, cle(nom))).fetchone()["rendu"]
    except psycopg.Error as e:
        log.warning("boucle %s : verrou non relâché explicitement (%s) — la fermeture de "
                    "la connexion le rend", nom, e)
        return
    if not rendu:
        log.warning("boucle %s : le verrou n'était plus tenu en fin de tour — la "
                    "connexion a été perdue pendant le tour", nom)


@contextmanager
def tour(nom: str) -> Iterator[bool]:
    """Tient la boucle `nom` le temps du bloc. Rend `True` si CE processus la tient, et
    `False` si un autre la tient : l'appelant saute alors son tour, sans rien faire.

    Synchrone, comme les tours qu'il protège (appelé dans le thread du tour, jamais sur
    la boucle d'événements). Une base injoignable LÈVE."""
    conn = _connexion()
    try:
        tenu = conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS tenu",
                            (_FAMILLE, cle(nom))).fetchone()["tenu"]
        if not tenu:
            niveau = logging.DEBUG if nom in _SAUTEES_DITES else logging.INFO
            _SAUTEES_DITES.add(nom)
            log.log(niveau, "boucle %s : tour sauté, un autre processus la tient", nom)
            yield False
            return
        _SAUTEES_DITES.discard(nom)
        try:
            yield True
        finally:
            _relacher(conn, nom)
    finally:
        conn.close()


def sous_verrou(nom: str, fonction: Callable[[], T], *, si_saute: T) -> T:
    """Joue UN tour de la boucle `nom` (`fonction`, synchrone) sous son verrou, ou rend
    `si_saute` sans rien jouer si un autre processus la tient. C'est la forme que les
    boucles passent à `asyncio.to_thread` : le verrou se prend et se rend dans le thread
    du tour, jamais sur la boucle d'événements."""
    with tour(nom) as tenu:
        return fonction() if tenu else si_saute

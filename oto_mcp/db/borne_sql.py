"""La borne `statement_timeout` du pool applicatif, et sa MESURE (#1232, lot 4).

Une borne globale du processus (`bornes_processus`), `OTO_MCP_DB_STATEMENT_TIMEOUT_MS`
(30 s ; `0` désarme) — sous le même interrupteur que la limite de débit
(`OTO_DEBIT_MODE`) :

- **Application** (`enforce`) : PostgreSQL la tient. `_conn._connect_options` pose
  `statement_timeout` sur chaque connexion du POOL ; une requête au-delà est annulée
  par le serveur (`QueryCanceled`). Les chemins qui posent déjà le leur le gardent
  (`SET LOCAL statement_timeout` d'une lecture bornée, d'un palmarès, d'une audience),
  et ceux qui ont le droit d'être longs en sont exemptés par construction : la
  préparation de la base au démarrage (`_init.apply_boot_schema`, `statement_timeout`
  remis à 0 pour sa transaction), le DDL à chaud (sa propre borne), le travail de fond
  hors pool (`_connect_autocommit(bornee=False)`), les révisions Alembic (leur moteur).
- **Mesure** (`report`, défaut) : PostgreSQL ne coupe RIEN — une borne côté base ne se
  « journalise sans tuer » que côté client. Le curseur du pool (`CurseurBorne`)
  chronomètre chaque ordre ; au-delà de la borne, s'il n'a pas posé de borne propre
  qui l'autorise (lue par `pg_settings`, sur la même connexion, SEULEMENT après un
  dépassement), c'est une requête qu'`enforce` aurait annulée. En application, une
  annulation au-delà de la borne est comptée comme servie.

Chaque dépassement va au palmarès des refus (limiteur `sql`, classe = la fonction
d'`oto_mcp` qui a lancé l'ordre, clé = empreinte du texte SQL — jamais ses paramètres)
et au journal. Le chemin ordinaire ne paie qu'une soustraction d'horloge.
"""
from __future__ import annotations

import logging
import sys
import time

import psycopg
from psycopg import rows

from .. import bornes_processus, limite_refus_top

logger = logging.getLogger(__name__)

#: Les modules traversés entre l'appelant et le curseur : jamais nommés comme appelant.
_PLOMBERIE = ("psycopg", "psycopg_pool", "contextlib", __name__, "oto_mcp.db._conn")


def _appelant() -> str:
    """`module:fonction` du premier cadre hors plomberie — un nom de code, jamais une
    donnée. Calculé seulement sur un dépassement."""
    cadre = sys._getframe(2)
    while cadre is not None:
        module = cadre.f_globals.get("__name__", "")
        if not module.startswith(_PLOMBERIE):
            return f"{module}:{cadre.f_code.co_name}"
        cadre = cadre.f_back
    return "?"


def _texte(requete) -> str:
    if isinstance(requete, bytes):
        return requete.decode("utf-8", "replace")
    if isinstance(requete, str):
        return requete
    return repr(requete)        # `sql.Composed` : sa forme, sans connexion à résoudre


def _borne_propre_ms(conn) -> int:
    """Le `statement_timeout` en vigueur sur `conn` (ms, `0` = aucun) — celui qu'un
    chemin a posé lui-même (`SET LOCAL`), ou celui de la connexion."""
    with conn.cursor(row_factory=rows.tuple_row) as cur:
        cur.execute("SELECT setting::int FROM pg_settings "
                    "WHERE name = 'statement_timeout'")
        return int(cur.fetchone()[0])


def constater(curseur, requete, duree_s: float, erreur) -> None:
    """Après un ordre : rien sous la borne ; au-delà, un dépassement si la borne du
    processus l'aurait coupé (cf. la docstring du module)."""
    r = bornes_processus.reglages()
    if not r.actif or r.sql_ms <= 0 or duree_s * 1000 < r.sql_ms:
        return
    if erreur is not None:
        # En application, une annulation au-delà de la borne est la nôtre (une borne
        # propre plus courte aurait coupé avant). En mesure, c'est celle du chemin.
        if not (r.applique and isinstance(erreur, psycopg.errors.QueryCanceled)):
            return
    else:
        if r.applique:
            return                  # passé malgré la borne posée : le chemin a la sienne
        try:
            propre = _borne_propre_ms(curseur.connection)
        except psycopg.Error:
            logger.warning("borne sql : lecture de statement_timeout en échec",
                           exc_info=True)
            return
        if propre and propre >= duree_s * 1000:
            return                  # le chemin a sa propre borne, qui l'autorise
    bornes_processus.depasser(
        limite_refus_top.SQL, _appelant(), limite_refus_top.empreinte(_texte(requete)),
        f"ordre SQL de {duree_s * 1000:.0f} ms (borne {r.sql_ms} ms)", r.mode)


class CurseurBorne(psycopg.Cursor):
    """Le curseur du pool applicatif : chronomètre `execute` et `executemany`."""

    def execute(self, query, params=None, **kwargs):
        debut, erreur = time.monotonic(), None
        try:
            return super().execute(query, params, **kwargs)
        except BaseException as e:
            erreur = e
            raise
        finally:
            constater(self, query, time.monotonic() - debut, erreur)

    def executemany(self, query, params_seq, **kwargs):
        debut, erreur = time.monotonic(), None
        try:
            return super().executemany(query, params_seq, **kwargs)
        except BaseException as e:
            erreur = e
            raise
        finally:
            constater(self, query, time.monotonic() - debut, erreur)

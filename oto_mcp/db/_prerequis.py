"""Les prérequis d'une base, vérifiés par le démarrage AVANT son premier ordre (#969).

Le démarrage crée le schéma d'une base neuve et pose deux extensions : `vector`
(pgvector — la recherche sémantique, `doc_embeddings`, `halfvec`/`hnsw`) et `pg_trgm`
(les index trigramme de la recherche transverse). Sur la base d'un tiers, ce qui manque se découvrait au milieu de la transaction, sous la forme d'une
erreur PostgreSQL brute (`permission denied`, `could not open extension control
file`) — le boot mourait sans dire ce que l'instance doit fournir.

Ce module dit ce qu'une base EXIGE, et refuse en le nommant
(`PrerequisBaseManquant`) :

- **chaque extension** : déjà installée, rien n'est exigé — c'est le cas de toute
  base existante, dont la nôtre, qui ne demande donc aucun droit de plus qu'avant.
  Absente, le serveur doit la proposer et le rôle doit pouvoir la créer (`vector`
  exige un superutilisateur ; `pg_trgm`, extension de confiance, le droit CREATE sur
  la base) ;
- **sur une base NEUVE seulement**, un schéma courant où le rôle peut créer : c'est
  là que naîtront toutes les tables. Une base existante n'est pas jugée ici — ses
  ordres sans rien à faire ne partent pas (`_ddl_garde`), et exiger d'elle un droit
  qu'elle n'utilise pas serait un refus que rien ne justifie.
"""
from __future__ import annotations

import logging

import psycopg

from ._version_alembic import EtatRegistre

logger = logging.getLogger(__name__)

# Ce que le démarrage crée, et pour quoi.
EXTENSIONS: dict[str, str] = {
    "vector": "pgvector — la recherche sémantique (`doc_embeddings`)",
    "pg_trgm": "les index trigramme de la recherche transverse",
}


class PrerequisBaseManquant(RuntimeError):
    """La base ne fournit pas ce que le démarrage exige ; le message dit quoi."""


def verifier(conn: psycopg.Connection, etat: EtatRegistre) -> None:
    """Refuse une base qui ne remplit pas ses prérequis, et pose les extensions qui
    manquent. À appeler sous le verrou du démarrage, avant tout ordre de schéma."""
    if etat is EtatRegistre.NEUVE:
        _verifier_schema_courant(conn)
    for extension, usage in EXTENSIONS.items():
        _poser_extension(conn, extension, usage)


def _verifier_schema_courant(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "SELECT current_schema() AS schema, current_user AS role, "
        "CASE WHEN current_schema() IS NULL THEN false "
        "ELSE has_schema_privilege(current_schema(), 'CREATE') END AS peut_creer"
    ).fetchone()
    if row["schema"] is None:
        raise PrerequisBaseManquant(
            "base neuve : aucun schéma courant (`search_path` ne désigne aucun schéma "
            f"existant pour le rôle {row['role']!r}) — le démarrage n'a nulle part où "
            "créer ses tables.")
    if not row["peut_creer"]:
        raise PrerequisBaseManquant(
            f"base neuve : le rôle {row['role']!r} n'a pas le droit CREATE sur le schéma "
            f"{row['schema']!r}, où le démarrage crée toutes ses tables. Prérequis : "
            f"`GRANT CREATE ON SCHEMA {row['schema']} TO {row['role']}` (ou un rôle "
            "propriétaire du schéma).")


def _poser_extension(conn: psycopg.Connection, extension: str, usage: str) -> None:
    if conn.execute("SELECT 1 FROM pg_extension WHERE extname = %s",
                    (extension,)).fetchone():
        return
    if not conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s",
                        (extension,)).fetchone():
        raise PrerequisBaseManquant(
            f"l'extension `{extension}` ({usage}) n'est pas disponible sur ce serveur "
            "PostgreSQL. Prérequis : un serveur qui la propose, ou l'extension déjà "
            "créée dans la base par un administrateur.")
    # Le seul moyen exact de savoir si CE rôle peut la créer (extension de confiance
    # ou non, superutilisateur ou non, droit CREATE sur la base ou non) est d'essayer :
    # dans un point de reprise, pour qu'un refus ne ruine pas la transaction du boot.
    try:
        with conn.transaction():
            conn.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    except psycopg.errors.InsufficientPrivilege as e:
        raise PrerequisBaseManquant(
            f"l'extension `{extension}` ({usage}) est absente de la base et le rôle n'a "
            f"pas le droit de la créer ({e.diag.message_primary}). Prérequis : "
            f"`CREATE EXTENSION {extension}` joué une fois par un administrateur de la "
            "base, ou un rôle qui en a le droit.") from e
    logger.info("prérequis de base : extension %s créée", extension)

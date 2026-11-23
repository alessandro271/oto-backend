"""La file d'indexation sémantique : réservations, échecs, consommation du jour.

Le worker `embed_worker` draine les sources marquées à indexer (pages, briefs, couches
de contexte, lignes de tableau opt-in). Trois choses tiennent ici, dans deux tables
(`db/schema/embeddings.py::EMBED_FILE`) :

- **la réservation** (`reserver`) : un bail en base, posé par un seul ordre
  `INSERT … ON CONFLICT DO UPDATE … WHERE bail échu`. Deux processus qui réservent la
  même source en même temps se sérialisent sur la clé primaire : le second relit le bail
  du premier et n'obtient rien. Les listes de sources à indexer écartent une source sous
  bail (`hors_bail_sql`). Le verrou de boucle (`verrou_de_boucle`) ne laisse déjà jouer
  qu'un tour à la fois ; le bail tient quand ce verrou est perdu en cours de tour
  (connexion rompue) ;
- **l'échec** : un échec passager se compte (`noter_passager`) sur le texte qui l'a
  produit ; un échec définitif (`noter_echec`) garde la raison, et l'appelant baisse le
  drapeau d'indexation de la source — elle sort de la file. Une nouvelle écriture de la
  source la remet dans la file ;
- **la consommation du jour** (UTC), en tokens facturés tels que le fournisseur les
  compte, et le plafond atteint, posé une seule fois par jour (`marquer_plafond`).
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

# Les noms de source du worker. Ils entrent dans du SQL (`hors_bail_sql`) : liste fermée.
SOURCES = ("doc", "brief", "node", "row")

_JOUR = "(NOW() AT TIME ZONE 'UTC')::date"


def _source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"source d'indexation inconnue : {source!r}")
    return source


def hors_bail_sql(source: str, ref_sql: str) -> str:
    """Le filtre SQL qui écarte d'une liste les sources de `source` sous bail.
    `ref_sql` : l'expression de la clé de la source dans la requête appelante."""
    return (f"NOT EXISTS (SELECT 1 FROM embed_sources s WHERE s.source = '{_source(source)}' "
            f"AND s.ref = ({ref_sql})::text AND s.bail_jusqu_a > NOW())")


def hors_echec_sql(source: str, ref_sql: str) -> str:
    """Le filtre SQL qui écarte les sources de `source` sorties de la file en échec :
    le re-marquage du démarrage (`db/_init.py`) ne les remet pas à indexer."""
    return (f"NOT EXISTS (SELECT 1 FROM embed_sources s WHERE s.source = '{_source(source)}' "
            f"AND s.ref = ({ref_sql})::text AND s.echec_raison IS NOT NULL)")


def reserver(source: str, refs: list[str], bail_s: int) -> set[str]:
    """Pose un bail de `bail_s` secondes sur chaque source de `refs` qui n'en porte pas
    déjà un en cours. Rend celles que CE processus tient."""
    if not refs:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "INSERT INTO embed_sources (source, ref, bail_jusqu_a) "
            "SELECT %s, r, NOW() + make_interval(secs => %s) FROM unnest(%s::text[]) AS r "
            "ON CONFLICT (source, ref) DO UPDATE SET bail_jusqu_a = EXCLUDED.bail_jusqu_a "
            "WHERE embed_sources.bail_jusqu_a IS NULL OR embed_sources.bail_jusqu_a <= NOW() "
            "RETURNING ref",
            (_source(source), bail_s, list(refs))).fetchall()
    return {r["ref"] for r in rows}


def prolonger(source: str, ref: str, bail_s: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE embed_sources SET bail_jusqu_a = NOW() + make_interval(secs => %s) "
                     "WHERE source = %s AND ref = %s", (bail_s, _source(source), ref))


def relacher(source: str, ref: str) -> None:
    """Rend le bail ; l'état d'échec éventuel reste."""
    with _connect() as conn:
        conn.execute("UPDATE embed_sources SET bail_jusqu_a = NULL "
                     "WHERE source = %s AND ref = %s", (_source(source), ref))


def reussir(source: str, ref: str) -> None:
    """La source est indexée : son état (bail, tentatives, échec) s'efface."""
    with _connect() as conn:
        conn.execute("DELETE FROM embed_sources WHERE source = %s AND ref = %s",
                     (_source(source), ref))


def noter_passager(source: str, ref: str, sha: str) -> int:
    """Compte un échec passager sur le texte `sha` ; un autre texte repart de 1. Rend le
    nombre d'échecs d'affilée sur ce texte."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO embed_sources (source, ref, sha, tentatives, echec_raison) "
            "VALUES (%s, %s, %s, 1, NULL) "
            "ON CONFLICT (source, ref) DO UPDATE SET "
            "tentatives = CASE WHEN embed_sources.sha = EXCLUDED.sha "
            "                  THEN embed_sources.tentatives + 1 ELSE 1 END, "
            "sha = EXCLUDED.sha RETURNING tentatives",
            (_source(source), ref, sha)).fetchone()
    return int(row["tentatives"])


def noter_echec(source: str, ref: str, sha: str, raison: str) -> None:
    """La source sort de la file en échec, avec sa raison (l'appelant baisse son drapeau)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO embed_sources (source, ref, sha, echec_raison, echec_at) "
            "VALUES (%s, %s, %s, %s, NOW()) "
            "ON CONFLICT (source, ref) DO UPDATE SET sha = EXCLUDED.sha, "
            "echec_raison = EXCLUDED.echec_raison, echec_at = EXCLUDED.echec_at",
            (_source(source), ref, sha, raison[:2000]))


def echec(source: str, ref: str) -> Optional[dict]:
    """L'état noté d'une source (bail, tentatives, échec), ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT sha, tentatives, echec_raison, echec_at FROM embed_sources "
            "WHERE source = %s AND ref = %s", (_source(source), ref)).fetchone()
    return dict(row) if row else None


def jour() -> dict:
    """La consommation du jour (UTC) : `tokens` et `plafond_atteint` (bool)."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT tokens, plafond_atteint_at FROM embed_consommation WHERE jour = {_JOUR}"
        ).fetchone()
    if not row:
        return {"tokens": 0, "plafond_atteint": False}
    return {"tokens": int(row["tokens"]),
            "plafond_atteint": row["plafond_atteint_at"] is not None}


def ajouter_tokens(n: int) -> int:
    """Ajoute `n` tokens facturés au jour ; rend le total du jour."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO embed_consommation (jour, tokens) VALUES ({_JOUR}, %s) "
            "ON CONFLICT (jour) DO UPDATE SET tokens = embed_consommation.tokens + EXCLUDED.tokens "
            "RETURNING tokens", (int(n),)).fetchone()
    return int(row["tokens"])


def marquer_plafond() -> bool:
    """Pose « plafond atteint » pour le jour. True pour le seul appel qui l'a posé
    (c'est lui qui alerte), False s'il l'était déjà."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO embed_consommation (jour, tokens, plafond_atteint_at) "
            f"VALUES ({_JOUR}, 0, NOW()) "
            "ON CONFLICT (jour) DO UPDATE SET plafond_atteint_at = NOW() "
            "WHERE embed_consommation.plafond_atteint_at IS NULL RETURNING jour").fetchone()
    return row is not None

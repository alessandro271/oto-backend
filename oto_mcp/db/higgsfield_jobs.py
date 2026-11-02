"""Higgsfield jobs submitted on the platform key — table `higgsfield_jobs`
(DDL: `schema/usage.py::HIGGSFIELD_JOBS`).

Higgsfield charges a job only once it COMPLETES (failed, nsfw and canceled jobs are
free), and a job's status carries no price. This table records the price checked at
submission, so the read of the completed job bills it under the org that submitted it
(`tools/higgsfield.py`), and a job nobody reads back is still found:
`higgsfield_releve.py` settles it.

NOT flattened into `db.__init__` (`noter`, `lire` and `marquer` are too common for that
surface) — callers write `db.higgsfield_jobs.<fn>`.

⚠️ Synchronous (psycopg): an async caller goes through `run_in_threadpool`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

#: How long a settled job stays in the table: the journal keeps the billed line.
RETENTION_JOURS = 120


def noter(request_id: str, *, org_id: Optional[int], sub: Optional[str],
          run_id: Optional[str], model: str, prix_microdollars: int,
          approximatif: bool) -> None:
    """The job was submitted. A second write of the same job keeps the first: an
    `idempotency_key` sent again returns the original job, at its original price."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO higgsfield_jobs (request_id, org_id, sub, run_id, model, "
            "prix_microdollars, approximatif) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (request_id) DO NOTHING",
            (request_id, org_id, sub, run_id, model, int(prix_microdollars),
             bool(approximatif)))


def lire(request_id: str) -> Optional[dict]:
    """The recorded job, or None for a job submitted outside this table."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM higgsfield_jobs WHERE request_id = %s",
                           (request_id,)).fetchone()
    return dict(row) if row else None


def marquer(request_id: str, *, statut: str) -> None:
    """The job's final state. The first one stays: the biller keeps one line per job."""
    with _connect() as conn:
        conn.execute(
            "UPDATE higgsfield_jobs SET releve_le = NOW(), statut = %s "
            "WHERE request_id = %s AND releve_le IS NULL",
            (statut, request_id))


def a_relever(limite: int = 500) -> list[dict]:
    """The jobs without a final state, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM higgsfield_jobs WHERE releve_le IS NULL "
            "ORDER BY soumis_le LIMIT %s", (limite,)).fetchall()
    return [dict(r) for r in rows]


def purger(jours: int = RETENTION_JOURS) -> int:
    """Drop the SETTLED jobs older than `jours`. A job without a state is never dropped."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM higgsfield_jobs WHERE releve_le IS NOT NULL "
            "AND soumis_le < NOW() - make_interval(days => %s)", (jours,))
    return cur.rowcount

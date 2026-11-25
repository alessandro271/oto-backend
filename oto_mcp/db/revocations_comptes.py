"""L'instant « révoqué avant » d'un compte (`revocations_comptes`).

`poser` le pousse à maintenant (jamais en arrière) ; `lire` le rend. Le refus d'un jeton
émis avant cet instant vit dans `session_revoquee.py`, et la lecture servie à chaque
requête est celle de la garde d'identité (`db.get_suspension`), pas `lire`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def poser(sub: str, motif: str) -> float:
    """Pose « révoqué avant » = maintenant (horloge de la base, `clock_timestamp()`)
    pour `sub`, sans jamais reculer un instant déjà posé. Rend l'instant retenu, en
    secondes depuis l'époque (la fabrique de lignes rendrait une date tronquée à la
    seconde)."""
    with _connect() as conn:
        r = conn.execute(
            "INSERT INTO revocations_comptes (sub, revoque_avant, motif) "
            "VALUES (%s, clock_timestamp(), %s) "
            "ON CONFLICT (sub) DO UPDATE SET "
            "revoque_avant = GREATEST(revocations_comptes.revoque_avant, "
            "EXCLUDED.revoque_avant), motif = EXCLUDED.motif "
            "RETURNING EXTRACT(EPOCH FROM revoque_avant)::float8 AS avant",
            (sub, motif)).fetchone()
    return r["avant"]


def lire(sub: str) -> Optional[float]:
    """L'instant posé pour `sub` (secondes depuis l'époque), ou None."""
    with _connect() as conn:
        r = conn.execute("SELECT EXTRACT(EPOCH FROM revoque_avant)::float8 AS avant "
                         "FROM revocations_comptes WHERE sub = %s", (sub,)).fetchone()
    return r["avant"] if r else None

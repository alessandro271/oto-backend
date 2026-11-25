"""L'empreinte des facteurs de double authentification d'un compte (`empreintes_2fa`).

Trois gestes, par compte : `lire` (None quand rien n'a encore été posé), `poser`
(idempotent, remplace) et `poser_si_absente` (n'écrit que là où rien n'est posé — la pose
d'amorce de l'exploitant, qui ne doit jamais écraser ce que le webhook a posé). Le calcul de l'empreinte et ce qu'un changement déclenche vivent dans
`auth/revocation_2fa.py`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def lire(sub: str) -> Optional[str]:
    """L'empreinte posée pour `sub`, ou None si aucune ne l'a encore été."""
    with _connect() as conn:
        r = conn.execute("SELECT empreinte FROM empreintes_2fa WHERE sub = %s",
                         (sub,)).fetchone()
    return r["empreinte"] if r else None


def poser(sub: str, empreinte: str) -> None:
    """Pose `empreinte` pour `sub`, en remplaçant celle d'avant."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO empreintes_2fa (sub, empreinte) VALUES (%s, %s) "
            "ON CONFLICT (sub) DO UPDATE SET empreinte = EXCLUDED.empreinte, "
            "vue_le = NOW()", (sub, empreinte))


def poser_si_absente(sub: str, empreinte: str) -> bool:
    """Pose `empreinte` pour `sub` s'il n'en a aucune. Rend True si elle a été posée,
    False si une empreinte l'était déjà (laissée telle quelle)."""
    with _connect() as conn:
        r = conn.execute(
            "INSERT INTO empreintes_2fa (sub, empreinte) VALUES (%s, %s) "
            "ON CONFLICT (sub) DO NOTHING RETURNING sub", (sub, empreinte)).fetchone()
    return r is not None


def comptes_sans_empreinte(apres: str, n: int) -> list[str]:
    """Une page d'au plus `n` comptes de l'annuaire primaire (sub sans `:`) qui ont une
    ligne `users` et aucune empreinte, au-delà de `apres` dans l'ordre des sub. Lecture
    seule, bornée (`statement_timeout` 15 s)."""
    with _connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '15s'")
        rows = conn.execute(
            "SELECT u.sub FROM users u WHERE u.sub > %s AND strpos(u.sub, ':') = 0 "
            "AND NOT EXISTS (SELECT 1 FROM empreintes_2fa e WHERE e.sub = u.sub) "
            "ORDER BY u.sub LIMIT %s", (apres, int(n))).fetchall()
    return [r["sub"] for r in rows]

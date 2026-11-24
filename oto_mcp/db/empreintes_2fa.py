"""L'empreinte des facteurs de double authentification d'un compte (`empreintes_2fa`).

Deux gestes, par compte : `lire` (None quand rien n'a encore été posé) et `poser`
(idempotent). Le calcul de l'empreinte et ce qu'un changement déclenche vivent dans
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

"""Lire les mouvements du budget d'unités comme les lit le curseur : sous l'horizon.

`db_credits.page_de_mouvements` ne rend un mouvement qu'une fois validé SOUS
`pg_snapshot_xmin(pg_current_snapshot())`, l'horizon du cluster entier : toute
transaction en cours qui porte un xid le retient, y compris celle d'un autre worker
xdist sur une autre base du même PostgreSQL. Lu juste après l'écriture, un mouvement
reste donc invisible tant qu'une transaction plus ancienne court. C'est le contrat du
curseur (ne jamais sauter un mouvement validé en retard), pas une panne : le banc
attend l'horizon, avec une borne, puis compare exactement.

Rouge du 20/10/2026 (préprod) : un nouveau fichier de tests a redécoupé les parts de
la CI, et le banc du règlement s'est retrouvé à côté d'une transaction longue.
"""
from __future__ import annotations

import time

from oto_mcp.db import credits as db_credits

_DELAI_S = 30.0


def mouvements_sous_l_horizon(garder, attendus: int) -> list[dict]:
    """Les mouvements retenus par `garder`, relus jusqu'à en voir `attendus`.

    Passé `_DELAI_S`, lève : une transaction reste ouverte sur le cluster (une fuite
    de connexion d'un banc, typiquement), et le dire vaut mieux qu'une liste vide."""
    fin = time.monotonic() + _DELAI_S
    while True:
        lus = [m for m in db_credits.page_de_mouvements(0, 0, 1000)["mouvements"]
               if garder(m)]
        if len(lus) >= attendus:
            return lus
        if time.monotonic() > fin:
            raise AssertionError(
                f"{len(lus)} mouvement(s) sous l'horizon après {_DELAI_S:.0f} s, "
                f"{attendus} attendu(s) : une transaction reste ouverte sur le cluster "
                "et retient pg_snapshot_xmin.")
        time.sleep(0.05)

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
Rouge du 26/10/2026 (préprod, #1391) : même mécanisme, porteur trouvé = le vivier de
`tests/datastore/test_thin_read_cte_980.py` (6 000 insertions dans UNE transaction,
98 s en CI). Depuis, l'échec NOMME le porteur de l'horizon (`_porteur_de_l_horizon`).
"""
from __future__ import annotations

import time

from oto_mcp.db import credits as db_credits
from oto_mcp.db._conn import _connect

_DELAI_S = 30.0

# La transaction en cours la plus ancienne du cluster, hors soi : son xid (si elle a
# écrit) ou son instantané retient l'horizon. Métadonnées seulement, requête tronquée.
_PORTEUR_SQL = """
SELECT pid, datname, application_name, state,
       extract(epoch FROM now() - xact_start)::int AS age_s,
       left(regexp_replace(query, '\\s+', ' ', 'g'), 120) AS requete
  FROM pg_stat_activity
 WHERE pid <> pg_backend_pid()
   AND (backend_xid IS NOT NULL OR backend_xmin IS NOT NULL)
 ORDER BY greatest(age(backend_xid), age(backend_xmin)) DESC NULLS LAST
 LIMIT 1
"""


def _porteur_de_l_horizon() -> str:
    """Qui retient `pg_snapshot_xmin`, en une ligne lisible dans le rapport de CI."""
    try:
        with _connect() as conn:
            ligne = conn.execute(_PORTEUR_SQL).fetchone()
    except Exception as exc:  # le diagnostic ne doit jamais masquer l'échec qu'il décrit
        return f"porteur de l'horizon illisible (pg_stat_activity : {type(exc).__name__}: {exc})"
    if ligne is None:
        return "aucune autre transaction ouverte dans pg_stat_activity à cet instant"
    return (f"porteur de l'horizon : pid={ligne['pid']} base={ligne['datname']} "
            f"application={ligne['application_name']!r} état={ligne['state']} "
            f"transaction ouverte depuis {ligne['age_s']} s, requête={ligne['requete']!r}")


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
                f"et retient pg_snapshot_xmin. {_porteur_de_l_horizon()}.")
        time.sleep(0.05)

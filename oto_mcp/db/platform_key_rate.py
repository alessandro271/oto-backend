"""Le compteur de débit d'une org sur une clé PLATEFORME partagée (F-051).

Une ligne par (porteur, connecteur) dans `platform_key_rate` : la fenêtre FIXE en
cours et ses appels. La règle (combien, en quel mode) vit dans
`access/platform_rate.py` ; ce module ne fait que compter.
"""
from __future__ import annotations

from ._conn import _connect


def compter(scope: str, connector: str, window_s: int) -> int:
    """Compte UN appel de `scope` sur la clé `connector` dans la fenêtre en cours, et
    rend le total de cette fenêtre, cet appel compris.

    Une requête, atomique : l'upsert tient la ligne le temps de l'incrément, donc deux
    processus qui comptent en même temps ne perdent rien. La fenêtre se calcule sur
    l'horloge de la BASE (`clock_timestamp`), commune à tous les processus — jamais sur
    celle d'un processus. Changer de fenêtre remet le compte à 1 sur la même ligne."""
    w = max(1, int(window_s))
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO platform_key_rate (scope, connector, window_start, calls)
            VALUES (%s, %s,
                    to_timestamp(floor(extract(epoch FROM clock_timestamp()) / %s) * %s),
                    1)
            ON CONFLICT (scope, connector) DO UPDATE SET
                calls = CASE WHEN platform_key_rate.window_start = EXCLUDED.window_start
                             THEN platform_key_rate.calls + 1 ELSE 1 END,
                window_start = EXCLUDED.window_start
            RETURNING calls
            """,
            (scope, connector, w, w)).fetchone()
    return int(row["calls"]) if row else 0

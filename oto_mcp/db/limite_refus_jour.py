"""Le palmarès quotidien des refus des limiteurs (#1232, lot 1).

Une ligne par (jour, environnement, limiteur, classe, clé hachée) dans
`limite_refus_jour`, qui CUMULE les refus de la journée. La règle (quoi compter, quand
écrire) vit dans `limite_refus_top.py` ; ce module n'écrit, ne purge et ne lit que ça.
"""
from __future__ import annotations

from ._conn import _connect

#: Borne d'une écriture ou d'une purge : la mesure n'attend jamais la base longtemps.
_DUREE_MAX_MS = 5000

_AJOUTER = """
INSERT INTO limite_refus_jour (jour, env, limiteur, classe, cle, refus, mode,
                               premier_at, dernier_at)
VALUES (%(jour)s, %(env)s, %(limiteur)s, %(classe)s, %(cle)s, %(refus)s, %(mode)s,
        %(premier_at)s, %(dernier_at)s)
ON CONFLICT (jour, env, limiteur, classe, cle) DO UPDATE SET
    refus = limite_refus_jour.refus + EXCLUDED.refus,
    mode = EXCLUDED.mode,
    premier_at = LEAST(limite_refus_jour.premier_at, EXCLUDED.premier_at),
    dernier_at = GREATEST(limite_refus_jour.dernier_at, EXCLUDED.dernier_at)
"""


def ajouter(lignes: list[dict]) -> None:
    """Ajoute les refus d'une minute close aux cumuls du jour, en UNE transaction.
    Lignes triées par clé primaire : deux processus qui écrivent la même minute
    prennent leurs verrous de ligne dans le même ordre, sans interblocage."""
    if not lignes:
        return
    ordre = sorted(lignes, key=lambda l: (l["jour"], l["env"], l["limiteur"],
                                          l["classe"], l["cle"]))
    with _connect() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {_DUREE_MAX_MS}")
        with conn.cursor() as cur:
            cur.executemany(_AJOUTER, ordre)


def purger(retention_jours: int) -> None:
    """Retire les jours plus anciens que `retention_jours` (borne de la table)."""
    with _connect() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {_DUREE_MAX_MS}")
        conn.execute("DELETE FROM limite_refus_jour WHERE jour < CURRENT_DATE - %s",
                     (max(1, int(retention_jours)),))


def top(conn, since_days: int, n: int) -> list:
    """Les `n` premières clés par (jour, environnement, limiteur), sur les `since_days`
    derniers jours (aujourd'hui compris), du plus récent au plus ancien. Sur la
    connexion bornée de l'appelant (`usage.transport_refusal_stats`)."""
    return conn.execute(
        """
        SELECT jour, env, limiteur, classe, cle, refus, mode, premier_at, dernier_at,
               rang
        FROM (
            SELECT r.*, row_number() OVER (PARTITION BY jour, env, limiteur
                                           ORDER BY refus DESC, cle) AS rang
            FROM limite_refus_jour r
            WHERE jour > CURRENT_DATE - %s
        ) t
        WHERE rang <= %s
        ORDER BY jour DESC, env, limiteur, rang
        LIMIT 1000
        """,
        (int(since_days), int(n)),
    ).fetchall()

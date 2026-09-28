"""Les reveals de téléphone Apollo reçus par oto — table `apollo_phone_reveals`.

Le cycle d'une ligne, dans l'ordre où il se produit :

1. `ouvrir` — AVANT l'appel à Apollo : l'URL de réception existe dès qu'Apollo peut
   la POSTer, et il peut la POSTer avant que son propre appel ne nous rende la main.
2. `lier_request_id` — quand Apollo a rendu son `request_id` ; `abandonner` quand il
   n'a rien accepté (personne trouvée, ou appel en échec) : rien n'arrivera jamais.
3. `recevoir` — le POST d'Apollo. Le PREMIER corps est gardé ; une retentative
   n'écrase rien et ne fait que compter (`deliveries`).
4. `lire` — `apollo_reveal_phone_result`, par (org, request_id).
5. `purger` — la maintenance, au-delà de trente jours (la rétention d'Apollo).

Non aplati dans `db.*` (comme `outreach`) : `ouvrir`/`lire`/`purger` sont des noms trop
communs pour la surface plate. Les appelants écrivent
`from ..db import apollo_reveals as db_apollo`.

⚠️ Toutes ces fonctions sont SYNCHRONES (psycopg) : un appelant async passe par
`run_in_threadpool` (`docs/event-loop-perf.md`).
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from ._conn import _connect

#: Ce que rend `recevoir`, selon ce que la ligne était avant le POST.
RECU = "received"
DOUBLON = "duplicate"
INCONNU = "unknown"


def ouvrir(token_hash: str, org_id: Optional[int], sub: str) -> None:
    """La commande existe : son URL de réception est désormais valide 30 jours."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO apollo_phone_reveals (token_hash, org_id, sub) "
            "VALUES (%s, %s, %s)",
            (token_hash, org_id, sub))


def lier_request_id(token_hash: str, request_id: str) -> None:
    """Rattache l'identifiant qu'Apollo a rendu. Idempotent ; ne réécrit pas un id
    déjà posé par le POST (qui peut arriver le premier et le porter)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE apollo_phone_reveals SET request_id = COALESCE(request_id, %s) "
            "WHERE token_hash = %s",
            (request_id, token_hash))


def abandonner(token_hash: str) -> None:
    """Apollo n'a rien accepté : l'URL de réception cesse d'exister."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM apollo_phone_reveals WHERE token_hash = %s "
            "AND payload IS NULL",
            (token_hash,))


def recevoir(token_hash: str, payload: Any,
             request_id: Optional[str] = None) -> str:
    """Le POST d'Apollo. `RECU` au premier, `DOUBLON` ensuite, `INCONNU` si le jeton
    ne désigne aucune commande vivante (jamais émis, abandonné, ou expiré).

    Le premier corps GAGNE : une retentative d'Apollo porte le même contenu, et
    l'écraser ne ferait que changer, sous un lecteur, ce qu'il a déjà lu. Le
    `request_id` du corps ne complète la ligne que si l'appel ne l'a pas encore
    posé — c'est le cas du POST arrivé avant la fin de l'appel qui l'a commandé.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE apollo_phone_reveals SET "
            "  deliveries = deliveries + 1, "
            "  payload = COALESCE(payload, %s), "
            "  received_at = COALESCE(received_at, NOW()), "
            "  request_id = COALESCE(request_id, %s) "
            "WHERE token_hash = %s AND expires_at > NOW() "
            "RETURNING deliveries",
            (Jsonb(payload), request_id, token_hash)).fetchone()
    if row is None:
        return INCONNU
    return RECU if row["deliveries"] == 1 else DOUBLON


def lire(org_id: Optional[int], request_id: str) -> Optional[dict]:
    """La commande `request_id` de l'org, si elle est vivante — reçue ou pas encore.

    `None` : oto n'a jamais commandé ce reveal pour cette org (ou il a expiré).
    Sinon `{payload, received_at, deliveries}`, `payload` à `None` tant qu'Apollo
    n'a rien POSTé. L'org fait partie de la clé : un `request_id` connu d'une autre
    org ne se lit pas d'ici. Deux commandes au même identifiant (l'index n'est pas
    unique, cf. le DDL) : la livrée d'abord, puis la plus récente.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT payload, received_at, deliveries FROM apollo_phone_reveals "
            "WHERE org_id IS NOT DISTINCT FROM %s AND request_id = %s "
            "AND expires_at > NOW() "
            "ORDER BY (payload IS NOT NULL) DESC, created_at DESC LIMIT 1",
            (org_id, request_id)).fetchone()


def purger() -> int:
    """Retire les commandes au-delà de leur échéance. Rend le nombre retiré."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM apollo_phone_reveals WHERE expires_at <= NOW()")
        return cur.rowcount or 0


def compter_purgeables() -> int:
    """Ce que `purger` retirerait — pour la maintenance à blanc."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM apollo_phone_reveals "
            "WHERE expires_at <= NOW()").fetchone()
    return int(row["n"])

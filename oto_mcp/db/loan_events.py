"""Le journal des prêts de compte : `connector_loan_events` (docs/partage-de-compte.md).

La pose et le retrait d'un prêt (`capabilities/connectors/sharing`) y laissent une ligne,
écrite dans la MÊME transaction que le `share_side` qu'ils changent : un prêt posé sans
trace n'existe pas. Le journal générique des appels ne suffit pas — il ne voit pas le
corps d'un appel REST. Jamais de secret : le compte est nommé par sa ligne du coffre.
"""
from __future__ import annotations

LEND, REVOKE = "lend", "revoke"

_NOTER = """
INSERT INTO connector_loan_events (actor_sub, org_id, entity_type, entity_id, connector,
                                   account, target, action)
VALUES (%(actor_sub)s, %(org_id)s, %(entity_type)s, %(entity_id)s, %(connector)s,
        %(account)s, %(target)s, %(action)s)
"""


def noter(conn, *, actor_sub: str, org_id: int, entity_type: str, entity_id: str,
          connector: str, account: str, target: str, action: str) -> None:
    """Une ligne, sur la connexion (donc la transaction) de l'écriture du partage."""
    if action not in (LEND, REVOKE):
        raise ValueError(f"loan event: unknown action {action!r}")
    conn.execute(_NOTER, {"actor_sub": actor_sub, "org_id": int(org_id),
                          "entity_type": entity_type, "entity_id": entity_id,
                          "connector": connector, "account": account or "",
                          "target": target, "action": action})


def suivre_la_ligne(conn, entity_type: str, ancien: str, nouveau: str, connector: str,
                    account: str) -> None:
    """Le journal suit SA ligne du coffre quand une fusion de comptes la rechiffre sous un
    nouveau nom (`credentials_store.rekey_personal_credentials`) : la lecture par ligne
    (`pour_la_ligne`) garde son historique. Une ligne laissée en place garde le sien."""
    conn.execute(
        "UPDATE connector_loan_events SET entity_id = %s WHERE entity_type = %s "
        "AND entity_id = %s AND connector = %s AND account = %s",
        (nouveau, entity_type, ancien, connector, account or ""))


def pour_la_ligne(entity_type: str, entity_id: str, connector: str,
                  account: str = "", limit: int = 50) -> list[dict]:
    """Les gestes sur UNE ligne du coffre, du plus récent au plus ancien."""
    from ._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT at, actor_sub, org_id, entity_type, entity_id, connector, account, "
            "target, action FROM connector_loan_events WHERE entity_type = %s "
            "AND entity_id = %s AND connector = %s AND account = %s "
            "ORDER BY id DESC LIMIT %s",
            (entity_type, entity_id, connector, account or "", int(limit))).fetchall()


def dernieres_poses(lignes: list[tuple[str, str, str, str]]) -> dict[tuple, object]:
    """`{(entity_type, entity_id, connector, account, target): at}` — la date de la
    DERNIÈRE pose de chaque cible, pour un lot de lignes du coffre, en une requête (index
    `idx_loan_events_ligne`). Une cible absente n'a pas de pose journalisée : le journal
    est né après elle, sa date n'est pas connue."""
    uniques = sorted({(t, str(i), c, a or "") for t, i, c, a in lignes})
    if not uniques:
        return {}
    from ._conn import _connect
    clause = ", ".join(["(%s, %s, %s, %s)"] * len(uniques))
    params: list = [v for ligne in uniques for v in ligne]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_id, connector, account, target, MAX(at) AS at "
            "FROM connector_loan_events WHERE action = 'lend' AND "
            f"(entity_type, entity_id, connector, account) IN ({clause}) "
            "GROUP BY entity_type, entity_id, connector, account, target",
            params).fetchall()
    return {(r["entity_type"], r["entity_id"], r["connector"], r["account"], r["target"]):
            r["at"] for r in rows}

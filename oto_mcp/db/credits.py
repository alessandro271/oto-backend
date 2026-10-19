"""Le stockage du budget d'unités : barème, modes de payeur, soldes, réservations et
mouvements (`db/schema/credits.py`).

La règle (qui paie, combien réserver, quand refuser, quel mode) vit dans
`access/credits.py` ; ce module ne fait qu'écrire et relire, en transactions courtes.
Une unité est un ENTIER : rien ici ne connaît de monnaie.

- **Verser** (commerce) : un mouvement idempotent par `(payeur, clé)` ; la même clé avec
  un autre contenu lève `CleEnConflit`.
- **Réserver** (moteur) : 2 écritures, une transaction — la réservation (`call_uid`
  unique, idempotente), puis le solde, verrouillé par sa ligne. En application, le solde
  n'est jamais engagé au-delà du disponible : la réservation qui ne tient pas est
  annulée en entier.
- **Régler** : 3 écritures — la réservation close, le solde, le mouvement `settle`.
- **Libérer** : 2 écritures, sans mouvement (rien n'a été consommé).
- **Curseur** (commerce) : les mouvements validés sous l'horizon
  `pg_snapshot_xmin(pg_current_snapshot())`, par `(xid, id)` : une transaction encore
  ouverte ne peut plus valider un mouvement EN DESSOUS de ce qui a été servi.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ._conn import _connect

# Les états d'une réservation.
OUVERTE, REGLEE, LIBEREE, ECHUE = "open", "settled", "released", "expired"

# Les colonnes d'un mouvement rendues au commerce (`xid` en texte : un xid8 dépasse le
# JSON d'un entier signé sur 53 bits).
_MOUVEMENT = ("id, xid::text AS xid, payer, kind, amount, held, reason, idempotency_key, "
              "call_uid, connector, function, quantity, meta, created_at")


class CleEnConflit(ValueError):
    """La même clé d'idempotence, pour le même payeur, avec un autre contenu."""


class BaremeEnConflit(ValueError):
    """Une version de barème déjà posée, avec d'autres lignes : une version ne se
    réécrit pas."""


@dataclass(frozen=True)
class Reservation:
    """L'issue d'une réservation. `posee` = cette demande a créé la réservation ;
    `deja` = une réservation existait déjà pour ce `call_uid` (rien n'a bougé) ;
    `insuffisant` = le disponible ne couvrait pas `reserved` (en application, rien n'a
    été écrit ; forcée, la réservation est posée quand même). `disponible` = balance −
    holds_open AVANT la réservation."""
    posee: bool
    deja: bool
    insuffisant: bool
    disponible: int


# ── le barème ────────────────────────────────────────────────────────────────

def poser_bareme(version: int, lignes: list[dict]) -> bool:
    """Pose la version `version` du barème, d'un bloc. Rend True si elle est neuve,
    False si la même version portait déjà exactement ces lignes. Lève `BaremeEnConflit`
    si elle en portait d'autres."""
    cles = sorted((l["connector"], l["function"], l["unit"], int(l["budget"]),
                   int(l["per_n"])) for l in lignes)
    with _connect() as conn, conn.transaction():
        neuve = conn.execute(
            "INSERT INTO credit_scales (version) VALUES (%s) ON CONFLICT DO NOTHING "
            "RETURNING version", (version,)).fetchone()
        if not neuve:
            existantes = sorted(
                (r["connector"], r["function"], r["unit"], int(r["budget"]),
                 int(r["per_n"])) for r in conn.execute(
                    "SELECT connector, function, unit, budget, per_n FROM credit_scale_lines "
                    "WHERE version = %s", (version,)).fetchall())
            if existantes != cles:
                raise BaremeEnConflit(f"la version {version} du barème est déjà posée, "
                                      "avec d'autres lignes")
            return False
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO credit_scale_lines (version, connector, function, unit, "
                "budget, per_n) VALUES (%s, %s, %s, %s, %s, %s)",
                [(version, *c) for c in cles])
    return True


def version_du_bareme() -> Optional[int]:
    """La version qui fait foi (la plus haute), ou None sans barème."""
    with _connect() as conn:
        row = conn.execute("SELECT max(version) AS v FROM credit_scales").fetchone()
    return int(row["v"]) if row and row["v"] is not None else None


def lignes_du_bareme(version: int) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT connector, function, unit, budget, per_n FROM credit_scale_lines "
            "WHERE version = %s ORDER BY connector, function, unit", (version,)).fetchall()]


# ── qui paie ─────────────────────────────────────────────────────────────────

def poser_mode(org_id: int, mode: str) -> None:
    """`org` retire la ligne (le défaut), `person` la pose."""
    with _connect() as conn:
        if mode == "org":
            conn.execute("DELETE FROM credit_payer_modes WHERE org_id = %s", (org_id,))
        else:
            conn.execute(
                "INSERT INTO credit_payer_modes (org_id, mode) VALUES (%s, %s) "
                "ON CONFLICT (org_id) DO UPDATE SET mode = EXCLUDED.mode, set_at = NOW()",
                (org_id, mode))


def orgs_en_mode_personne() -> set[int]:
    with _connect() as conn:
        return {int(r["org_id"]) for r in conn.execute(
            "SELECT org_id FROM credit_payer_modes WHERE mode = 'person'").fetchall()}


# ── le commerce verse, relit ─────────────────────────────────────────────────

def verser(payer: str, amount: int, reason: str, idempotency_key: str) -> tuple[dict, bool]:
    """Verse un mouvement `deposit` (signé) au payeur, une fois par clé. Rend
    `(mouvement, neuf)` ; la même clé avec le même contenu rend le mouvement d'origine,
    `neuf=False`. Lève `CleEnConflit` si son contenu diffère."""
    with _connect() as conn, conn.transaction():
        row = conn.execute(
            "INSERT INTO credit_movements (payer, kind, amount, reason, idempotency_key) "
            "VALUES (%s, 'deposit', %s, %s, %s) "
            "ON CONFLICT (payer, idempotency_key) WHERE idempotency_key IS NOT NULL "
            f"DO NOTHING RETURNING {_MOUVEMENT}",
            (payer, amount, reason, idempotency_key)).fetchone()
        if row is None:
            deja = conn.execute(
                f"SELECT {_MOUVEMENT} FROM credit_movements "
                "WHERE payer = %s AND idempotency_key = %s",
                (payer, idempotency_key)).fetchone()
            if (deja["kind"], int(deja["amount"]), deja["reason"]) != ("deposit", amount,
                                                                       reason):
                raise CleEnConflit(f"la clé {idempotency_key!r} a déjà servi pour {payer}, "
                                   "avec un autre mouvement")
            return dict(deja), False
        conn.execute(
            "INSERT INTO credit_balances (payer, balance) VALUES (%s, %s) "
            "ON CONFLICT (payer) DO UPDATE SET balance = credit_balances.balance + "
            "EXCLUDED.balance, updated_at = NOW()", (payer, amount))
    return dict(row), True


def solde(payer: str) -> dict:
    """`{balance, holds_open, as_of}` ; un payeur sans ligne a un solde nul."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT balance, holds_open, NOW() AS as_of FROM credit_balances "
            "WHERE payer = %s", (payer,)).fetchone()
        if row is None:
            row = conn.execute("SELECT 0 AS balance, 0 AS holds_open, NOW() AS as_of"
                               ).fetchone()
    return dict(row)


def mouvements_apres(xid: int, id_: int, limite: int) -> list[dict]:
    """Les mouvements d'après `(xid, id)`, validés sous l'horizon, par `(xid, id)`."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_MOUVEMENT} FROM credit_movements "
            "WHERE xid < pg_snapshot_xmin(pg_current_snapshot()) "
            "AND (xid, id) > (%s::text::xid8, %s) ORDER BY xid, id LIMIT %s",
            (str(xid), id_, limite)).fetchall()]


# ── le moteur réserve, règle, libère ─────────────────────────────────────────

class _Annuler(Exception):
    """Sort de la transaction de réservation sans rien écrire."""


def reserver(*, call_uid: str, payer: str, connector: str, function: str, unit: str,
             scale_version: int, quantity: int, reserved: int, deadline_s: int,
             forcer: bool) -> Reservation:
    """Réserve `reserved` unités pour l'appel `call_uid`, échéance dans `deadline_s`.

    `forcer=False` (application) : refuse — sans rien écrire — ce que le disponible ne
    couvre pas. `forcer=True` (mesure) : pose la réservation quand même, et dit
    `insuffisant`."""
    disponible = 0
    try:
        with _connect() as conn, conn.transaction():
            neuve = conn.execute(
                "INSERT INTO credit_holds (call_uid, payer, connector, function, unit, "
                "scale_version, quantity, reserved, deadline) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s)) "
                "ON CONFLICT (call_uid) DO NOTHING RETURNING call_uid",
                (call_uid, payer, connector, function, unit, scale_version, quantity,
                 reserved, deadline_s)).fetchone()
            if neuve is None:
                return Reservation(posee=False, deja=True, insuffisant=False, disponible=0)
            row = conn.execute(
                "INSERT INTO credit_balances (payer, holds_open) VALUES (%s, %s) "
                "ON CONFLICT (payer) DO UPDATE SET holds_open = credit_balances.holds_open "
                "+ EXCLUDED.holds_open, updated_at = NOW() "
                "RETURNING balance, holds_open", (payer, reserved)).fetchone()
            disponible = int(row["balance"]) - int(row["holds_open"]) + reserved
            insuffisant = disponible < reserved
            if insuffisant and not forcer:
                raise _Annuler()
    except _Annuler:
        return Reservation(posee=False, deja=False, insuffisant=True, disponible=disponible)
    return Reservation(posee=True, deja=False, insuffisant=insuffisant, disponible=disponible)


def regler(call_uid: str, *, montant: Optional[int], quantite: Optional[int],
           meta: dict, etat: str = REGLEE) -> Optional[dict]:
    """Règle la réservation ouverte `call_uid` à `montant` unités (None : au montant
    réservé, et `quantite` None : à la quantité réservée) : la réservation se ferme
    (`etat`), le solde perd `montant` et libère le réservé, un mouvement `settle` le
    dit. Rend la réservation close, ou None si elle n'était plus ouverte (déjà réglée,
    libérée ou échue : rien n'a bougé)."""
    with _connect() as conn, conn.transaction():
        hold = conn.execute(
            "UPDATE credit_holds SET state = %s, closed_at = NOW() "
            "WHERE call_uid = %s AND state = 'open' "
            "RETURNING payer, connector, function, unit, quantity, reserved, scale_version",
            (etat, call_uid)).fetchone()
        if hold is None:
            return None
        if montant is None:
            montant = int(hold["reserved"])
        if quantite is None:
            quantite = int(hold["quantity"])
        conn.execute(
            "UPDATE credit_balances SET balance = balance - %s, "
            "holds_open = holds_open - %s, updated_at = NOW() WHERE payer = %s",
            (montant, hold["reserved"], hold["payer"]))
        conn.execute(
            "INSERT INTO credit_movements (payer, kind, amount, held, call_uid, connector, "
            "function, quantity, meta, idempotency_key) VALUES "
            "(%s, 'settle', %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
            (hold["payer"], -montant, -int(hold["reserved"]), call_uid, hold["connector"],
             hold["function"], quantite,
             json.dumps({**meta, "scale_version": hold["scale_version"]}),
             f"settle:{call_uid}"))
    return dict(hold)


def liberer(call_uid: str) -> Optional[dict]:
    """Libère la réservation ouverte `call_uid` sans rien consommer. Rend la réservation
    close, ou None si elle n'était plus ouverte."""
    with _connect() as conn, conn.transaction():
        hold = conn.execute(
            "UPDATE credit_holds SET state = 'released', closed_at = NOW() "
            "WHERE call_uid = %s AND state = 'open' RETURNING payer, reserved",
            (call_uid,)).fetchone()
        if hold is None:
            return None
        conn.execute(
            "UPDATE credit_balances SET holds_open = holds_open - %s, updated_at = NOW() "
            "WHERE payer = %s", (hold["reserved"], hold["payer"]))
    return dict(hold)


def reservation(call_uid: str) -> Optional[dict]:
    """La réservation `call_uid`, quel que soit son état, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT call_uid, payer, connector, function, unit, quantity, reserved, "
            "scale_version, state FROM credit_holds WHERE call_uid = %s",
            (call_uid,)).fetchone()
    return dict(row) if row else None


def attendre_la_releve(call_uid: str, job: dict, deadline_s: int) -> bool:
    """Un travail ASYNCHRONE soumis : la réservation reste ouverte, avec sa relève
    (`job` : fonction de relève, ses arguments, son délai) et une échéance neuve.
    Rend False si elle n'était plus ouverte."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE credit_holds SET job = %s::jsonb, "
            "deadline = NOW() + make_interval(secs => %s) "
            "WHERE call_uid = %s AND state = 'open' RETURNING call_uid",
            (json.dumps(job), deadline_s, call_uid)).fetchone()
    return row is not None


def reservation_de_la_releve(function: str, args: dict) -> Optional[dict]:
    """La réservation OUVERTE dont la relève est `function(args)`, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT call_uid, payer, connector, function, unit, quantity, reserved, "
            "scale_version FROM credit_holds WHERE state = 'open' "
            "AND job->>'function' = %s AND job->'args' = %s::jsonb",
            (function, json.dumps(args))).fetchone()
    return dict(row) if row else None


def reservations_echues(limite: int) -> list[str]:
    """Les `call_uid` des réservations ouvertes passées leur échéance, les plus
    anciennes d'abord."""
    with _connect() as conn:
        return [r["call_uid"] for r in conn.execute(
            "SELECT call_uid FROM credit_holds WHERE state = 'open' AND deadline < NOW() "
            "ORDER BY deadline LIMIT %s", (limite,)).fetchall()]


# ── deux comptes d'une même personne ─────────────────────────────────────────

def suivre_le_solde(conn, old_sub: str, new_sub: str) -> bool:
    """Le solde personnel `user:<old_sub>` passe à `user:<new_sub>` quand les deux
    comptes fusionnent (`users.repointer_patrimoine`) : deux mouvements `transfer` le
    disent, et les réservations ouvertes suivent leur payeur. Le journal des mouvements
    ne se réécrit pas. Dans la transaction de l'appelant (`conn`). Rend True si un solde
    a bougé."""
    ancien, nouveau = f"user:{old_sub}", f"user:{new_sub}"
    row = conn.execute(
        "SELECT balance, holds_open FROM credit_balances WHERE payer = %s FOR UPDATE",
        (ancien,)).fetchone()
    if row is None or (not row["balance"] and not row["holds_open"]):
        return False
    b, h = int(row["balance"]), int(row["holds_open"])
    conn.execute("UPDATE credit_balances SET balance = 0, holds_open = 0, "
                 "updated_at = NOW() WHERE payer = %s", (ancien,))
    conn.execute(
        "INSERT INTO credit_balances (payer, balance, holds_open) VALUES (%s, %s, %s) "
        "ON CONFLICT (payer) DO UPDATE SET balance = credit_balances.balance + "
        "EXCLUDED.balance, holds_open = credit_balances.holds_open + EXCLUDED.holds_open, "
        "updated_at = NOW()", (nouveau, b, h))
    conn.execute("UPDATE credit_holds SET payer = %s WHERE payer = %s AND state = 'open'",
                 (nouveau, ancien))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO credit_movements (payer, kind, amount, held, reason, meta) "
            "VALUES (%s, 'transfer', %s, %s, 'account_merge', %s::jsonb)",
            [(ancien, -b, -h, json.dumps({"to": nouveau})),
             (nouveau, b, h, json.dumps({"from": ancien}))])
    return True

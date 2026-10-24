"""A key the member can use installs its connector — the selection follows the credentials.

**The rule** (22/10/2026). A connector EXPOSED to a member, in an org, that the member
has not installed yet is installed for them when a credential serves them there —
their own key, their active team's, the org's — or when it needs none (`secret_kind`
`none`: open data). It is the same rule as the first-session seeding
(org, active team, open data), held for EVERY member and every hosted run, at every
session, and no longer only for a member's very first session in an org.

**What it never does** — the member's gesture and the org's kit win:
- a connector the member PAUSED, or REMOVED (`connector_selection_removed`), stays so;
- a row already there (kit, push, the member's own install) keeps its provenance;
- nothing is ever paused or removed by this module;
- a keyless connector that spends another connector's key on its own (`web`, which
  falls back on serper: `Connector.spends_keys_of`) is not free — the member installs it;
- a key of the tenant or of the platform installs nothing (`platform`, `over_quota`,
  `tenant`): what they serve is offered by the catalogue, not mounted for everyone;
- an entry still pending (`pending_action`: a hosted channel whose account isn't
  linked yet) or absent from the snapshot installs nothing.

**How.** The selection table (`connectors/selection.py`) stays the mounting truth —
cheap, read at every handshake by `session_visibility`. This module adds to it from
the credential snapshot (`access.status_for`, the one `/api/me` serves), throttled per
`(sub, org)`: computing credentials at every `initialize` would cost a second per
session, and a runner opens one per call.

- `installs` — the rule, one connector at a time;
- `plan` — PURE: what to install, given the snapshot, the selection and the removals;
- `reconcile` — reads, plans, writes in ONE statement;
- `claim` / `run_if_due` / `invalidate` — the throttle: a marker per `(sub, org)`,
  claimed before computing (one session per burst), dropped by every vault write that
  can serve the member (`credentials_store._upsert`) so a new key shows at the next
  session, and aged out after `TTL` for what the vault doesn't see (joining a team,
  switching the active team, an org exposing a connector).

⚠️ A failure writes nothing and gives the turn back: the next session retries. The
handshake logs it as an error and mounts what the table already holds.

Convention: self-managing like `selection` (opens its own connection); `invalidate`
receives the caller's `conn` (it runs inside the vault write's transaction).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# The cascade levels whose key installs the connector (`access.status_for`): the
# member's own, their ACTIVE team's, their org's. Decided on oto-backend#1307 for the
# seeding: a key of the tenant or of the platform (`over_quota` included) installs
# nothing, nor a key of a team that isn't the active one (it doesn't resolve here).
_INSTALLING_MODES = frozenset({"user", "group", "org"})
# Nothing to bring: open data. NOT the cards that only hold optional settings
# (`settings`: the six waterfalls) — they serve through the caller's provider keys, and
# what was decided for the seeding (oto-backend#1307) is open data.
_KEYLESS = frozenset({"none"})

# How long a reconciliation holds when nothing in the vault moved. The vault invalidates
# on every write that can serve a member; this ages out what it can't see.
TTL = timedelta(minutes=30)

# Provenance of a row written by the rule (selection.ORIGINS): neither the member nor an
# org gesture placed it — the credential did. A kit removal never touches it (it removes
# `kit` rows only).
ORIGIN = "cle"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_selection_reconciled (
    sub           TEXT        NOT NULL,
    org_id        BIGINT      NOT NULL CONSTRAINT connector_selection_reconciled_org_reelle
                                       CHECK (org_id > 0),
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id)
);
"""


def init_schema(conn) -> None:
    """Creates the marker table. Idempotent, additive (boot DDL)."""
    conn.execute(_SCHEMA)


# --- the rule (pure) ------------------------------------------------------------

def installs(connector, entry: Optional[Mapping]) -> bool:
    """The rule for ONE connector. `connector` = its registry declaration (or None),
    `entry` = its row of `status_for(...)["providers"]` (or None).

    - nothing mounts for it (no namespace: a model key used by agents, never by a tool)
      → no;
    - `secret_kind` none (open data) → yes, whatever the snapshot says — unless it
      spends another connector's key on its own (`spends_keys_of`: `web` falls back on
      serper), which is not free: the member installs it;
    - `secret_kind` settings (a waterfall) → no: it serves through other keys;
    - no snapshot row, or a step still pending → no (not computed, or "one step left");
    - the member's key, their active team's or their org's answers → yes (a refused
      key included: it is there to be fixed, the agent should see the failure);
    - the tenant, the platform, nothing → no."""
    if connector is None or not getattr(connector, "namespaces", None):
        return False
    if connector.secret_kind in _KEYLESS:
        return not connector.spends_keys_of
    if connector.secret_kind == "settings":
        return False
    if entry is None or entry.get("pending_action"):
        return False
    return entry.get("mode") in _INSTALLING_MODES


def plan(snapshot: Mapping[str, Mapping], selection: Mapping[str, str],
         removed: Mapping[str, object], exposed: set[str], registry: Mapping) -> list[str]:
    """The connectors to install — PURE, the whole rule over a member's catalogue.

    Only connectors EXPOSED to the member in this org, with NO selection row (active or
    paused) and not removed by the member: installing an unexposed one would put back
    what the org cut, and a pause or a removal is the member's last word."""
    return [name for name in sorted(exposed)
            if name not in selection and name not in removed
            and installs(registry.get(name), snapshot.get(name))]


# --- the throttle -------------------------------------------------------------------

def due(sub: str, org_id: int) -> bool:
    """Must this `(sub, org)` be reconciled now? No marker, or one older than `TTL`."""
    from .. import db

    # The age is judged by the database (rows come back with timestamps as text here).
    with db._connect() as conn:
        row = conn.execute(
            "SELECT reconciled_at > NOW() - %s AS fresh FROM connector_selection_reconciled "
            "WHERE sub = %s AND org_id = %s",
            (TTL, sub, org_id),
        ).fetchone()
    return row is None or not row["fresh"]


def claim(sub: str, org_id: int) -> bool:
    """Takes this `(sub, org)`'s turn, in ONE statement: True when the caller must
    reconcile now, False when the marker is fresh — or another session just took it.

    Claimed BEFORE computing, and the claim IS the marker: on a first deploy (no marker
    anywhere) or after a key write that clears the markers, a fleet of hosted runs for
    the same person opens its sessions together, and each would otherwise pay its own
    snapshot for the same answer. One wins; the others mount what the table already
    holds. A key written WHILE the winner computes drops the marker it claimed, and the
    next session computes again — the winner never stamps after the fact. The winner
    gives the turn back if it fails (`release`)."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "INSERT INTO connector_selection_reconciled (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO UPDATE SET reconciled_at = NOW() "
            "WHERE connector_selection_reconciled.reconciled_at <= NOW() - %s "
            "RETURNING 1 AS won",
            (sub, org_id, TTL),
        ).fetchone()
    return row is not None


def release(sub: str, org_id: int) -> None:
    """Gives a claimed turn back (the reconciliation failed): the next session retries."""
    from .. import db

    with db._connect() as conn:
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s AND org_id = %s",
                     (sub, org_id))


def run_if_due(sub: str, org_id: int, group_id: Optional[int]) -> Optional[list[str]]:
    """The handshake's entry point: claim, reconcile, give the turn back on failure.
    Returns `None` when it wasn't this session's turn, else the connectors installed.
    Raises what `reconcile` raised (the caller logs it and mounts what the table holds)."""
    if not claim(sub, org_id):
        return None
    try:
        return reconcile(sub, org_id, group_id)
    except Exception:
        release(sub, org_id)
        raise


def invalidate(conn, entity_type: str, entity_id: str) -> None:
    """A vault row was written: the members it can serve are reconciled at their next
    session. Receives the WRITE's `conn` — same transaction, a write that rolls back
    invalidates nothing.

    `member` (`<org>:<sub>`) → that member in that org; `user` (an OAuth account, cross
    org) → that person everywhere; `org` → everyone in the org; `group` → the team's
    members in its org. `tenant` / `platform` → nobody: those keys install nothing
    (`_INSTALLING_MODES`). Any other kind is a vault the rule doesn't know: raised."""
    from .. import credentials_store as cs

    if entity_type == cs.MEMBER:
        org, sub = entity_id.split(":", 1)
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s AND org_id = %s",
                     (sub, int(org)))
    elif entity_type == cs.USER:
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s", (entity_id,))
    elif entity_type == cs.ORG:
        conn.execute("DELETE FROM connector_selection_reconciled WHERE org_id = %s",
                     (int(entity_id),))
    elif entity_type == "group":
        conn.execute(
            "DELETE FROM connector_selection_reconciled r USING org_group_members m, org_groups g "
            "WHERE m.group_id = %s AND g.id = m.group_id AND r.sub = m.sub AND r.org_id = g.org_id",
            (int(entity_id),))
    elif entity_type in (cs.TENANT, cs.PLATFORM):
        return
    else:
        raise ValueError(f"vault entity type {entity_type!r} unknown to the selection rule")


# --- the write ------------------------------------------------------------------------

def reconcile(sub: str, org_id: int, group_id: Optional[int]) -> list[str]:
    """Installs what the rule says for `(sub, org)`; returns the connectors installed.

    Raises on a snapshot or exposure read error — the CALLER decides (the handshake
    logs and mounts what is already there): nothing is written in that case. The marker
    is the caller's (`claim`), never stamped here."""
    from .. import access, db, providers
    from . import activation, selection as sel

    with db.reuse_connection():
        snapshot = access.status_for(sub, org=org_id, group=group_id)["providers"]
        exposed = activation.exposed_for(org_id, group_id)
        candidates = plan(snapshot, sel.list_selection(sub, org_id),
                          sel.list_removed(sub, org_id), exposed, providers.REGISTRY)
    if not candidates:
        return []
    # ONE statement, whatever the number of candidates. A row placed meanwhile (the
    # member's install or pause, a kit push) wins; a removal made meanwhile too.
    with db._connect() as conn:
        rows = conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
            "SELECT %s, %s, c, 'active', %s FROM unnest(%s::text[]) AS c "
            "WHERE NOT EXISTS (SELECT 1 FROM connector_selection_removed r "
            "                  WHERE r.sub = %s AND r.org_id = %s AND r.connector = c) "
            "ON CONFLICT (sub, org_id, connector) DO NOTHING "
            "RETURNING connector",
            (sub, org_id, ORIGIN, candidates, sub, org_id),
        ).fetchall()
    installed = sorted(r["connector"] for r in rows)
    if installed:
        logger.info("selection reconciled for %s in org %s: %d installed by a usable key",
                    sub, org_id, len(installed))
    return installed

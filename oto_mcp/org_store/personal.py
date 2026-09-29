"""L'org PERSONNELLE (`orgs.personal_of`) et le rattrapage de boot.

Depuis la suppression du « perso » org-less (ADR 0030 §8), tout user est toujours
dans une org : `ensure_personal_org` garantit son espace privé mono-membre et
qu'il a une org maison. `backfill_personal_orgs` le rejoue au boot, idempotent.

Étage 1 du package : consomme `orgs` (création) et `members` (adhésion, maison).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import members
from . import orgs
from ..db import _connect

_log = logging.getLogger(__name__)


def get_personal_org(sub: str) -> Optional[int]:
    """Org PERSO (privée, mono-membre) de `sub`, marquée `personal_of=sub`, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM orgs WHERE personal_of = %s AND archived_at IS NULL", (sub,)
        ).fetchone()
        return int(row["id"]) if row else None


def is_personal_org(org_id: int) -> bool:
    """True si l'org est un **espace personnel** (`personal_of` renseigné) — non
    supprimable (elle serait recréée au boot par `ensure_personal_org`)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT personal_of FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        return bool(row and row["personal_of"] is not None)


def _personal_label(email: Optional[str], name: Optional[str]) -> str:
    return (name or (email.split("@")[0] if email else None) or "Mon espace").strip() or "Mon espace"


def _reclaim_or_create_personal(sub: str, email: Optional[str], name: Optional[str]) -> int:
    """Récupère ou crée l'org perso de `sub`. **Réclamation SÛRE** : on ne marque une
    org existante comme perso QUE si c'est la SEULE org du user (mono-membre, créée par
    lui) — un user multi-org garde ses orgs partagées intactes, on lui crée une perso
    fraîche."""
    with _connect() as conn:
        # Auto-soin (couvre les DEUX branches, reclaim ET create) : une org perso
        # ARCHIVÉE détient encore le slot unique `uq_orgs_personal_of` tout en étant
        # invisible à `get_personal_org` (filtre `archived_at IS NULL`) → la relâcher
        # AVANT tout marquage, sinon UniqueViolation en boucle à chaque boot (vécu
        # 2026-07-01 : perso archivée → orgs orphelines recréées, une par boot ; la
        # collision frappait aussi bien la branche reclaim que la branche create).
        conn.execute(
            "UPDATE orgs SET personal_of = NULL "
            "WHERE personal_of = %s AND archived_at IS NOT NULL",
            (sub,),
        )
        row = conn.execute(
            """
            SELECT o.id FROM orgs o
             WHERE o.created_by = %s AND o.personal_of IS NULL AND o.archived_at IS NULL
               AND (SELECT count(*) FROM org_members m WHERE m.org_id = o.id) = 1
               AND EXISTS (SELECT 1 FROM org_members m WHERE m.org_id = o.id AND m.sub = %s)
               AND (SELECT count(*) FROM org_members m2 JOIN orgs o2 ON o2.id = m2.org_id
                     WHERE m2.sub = %s AND o2.archived_at IS NULL) = 1
             LIMIT 1
            """,
            (sub, sub, sub),
        ).fetchone()
        if row:
            oid = int(row["id"])
            conn.execute("UPDATE orgs SET personal_of = %s WHERE id = %s", (sub, oid))
            _log.info("ensure_personal_org: org #%s réclamée comme perso de %s", oid, sub)
            return oid
    oid = orgs.create_org(_personal_label(email, name), created_by=sub)
    members.add_org_member(oid, sub, org_role="org_admin", actor=None)  # le système
    with _connect() as conn:
        conn.execute("UPDATE orgs SET personal_of = %s WHERE id = %s", (sub, oid))
    _log.info("ensure_personal_org: org perso #%s créée pour %s", oid, sub)
    # Onboarding = un projet (ADR 0032 §7) : on sème le projet « Découverte » dans l'org
    # perso fraîchement créée (une seule fois, ici — pas sur la branche reclaim). Best-effort.
    from .. import discovery
    discovery.seed_for_org(sub, oid)
    return oid


def ensure_personal_org(sub: str, email: Optional[str] = None, name: Optional[str] = None) -> int:
    """Garantit l'**org perso** de `sub` (suppression du perso `org_id=0`) ET qu'il a une
    org active (la perso si aucune autre). Idempotent."""
    pid = get_personal_org(sub)
    if pid is None:
        pid = _reclaim_or_create_personal(sub, email, name)
    if members.get_active_org(sub) is None:   # nouveau user / ex-perso → la perso devient maison
        members.set_active_org(sub, pid)
    return pid


def ensure_members_personal_orgs(org_id: int) -> list[str]:
    """Garantit l'org perso de chaque membre de `org_id` qui n'en a plus ; rend leurs subs.

    À appeler APRÈS toute adhésion (`members.add_org_member`). Rejoindre l'org perso de
    quelqu'un lui retire son statut (`personal_of` remis à NULL : une org perso est
    mono-membre), et son ex-propriétaire se retrouvait SANS org perso jusqu'au prochain
    boot (`backfill_personal_orgs`). Or les projets privés (`owner_type='user'`) ne se
    listent que dans l'org perso (`ownership.perso_de_la_liste`) : « tous mes projets
    ont disparu » dès qu'un coéquipier acceptait son invitation (vécu 29/09/2026).

    Hors de la transaction de l'adhésion, et pour cause : tant qu'elle n'est pas
    validée, `personal_of` se lit encore sur l'org rejointe et `get_personal_org`
    croirait l'ex-propriétaire servi. Idempotent : un membre qui a son org perso n'est
    pas touché."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.sub, u.email, u.name FROM org_members m
              LEFT JOIN users u ON u.sub = m.sub
             WHERE m.org_id = %s
               AND NOT EXISTS (SELECT 1 FROM orgs o
                                WHERE o.personal_of = m.sub AND o.archived_at IS NULL)
            """,
            (org_id,),
        ).fetchall()
    for r in rows:
        ensure_personal_org(r["sub"], r.get("email"), r.get("name"))
        _log.info("org #%s : org perso rendue à %s (la sienne a été rejointe)",
                  org_id, r["sub"])
    return [r["sub"] for r in rows]


def backfill_personal_orgs() -> dict:
    """Idempotent (boot) : chaque user a une **org perso** marquée, et une org active
    (la perso si aucune autre).

    ⚠️ Ne TOUCHE PLUS aux ressources. La migration `owner_type='user'` → org perso qui
    vivait ici datait de la suppression du perso `org_id=0` ; depuis l'amendement ADR
    0030 §8 (2026-07-17) `owner_type='user'` n'est plus un vestige à rattraper mais le
    **scope membre** — un projet PRIVÉ rangé dans le contexte d'une org (`context_org_id`).
    Rejouée à chaque boot, elle DÉTRUISAIT ce scope : le projet privé quittait l'org de
    travail pour l'espace perso de son auteur → « mon projet a disparu » côté user (vécu
    2026-07-28, aucun projet `owner_type='user'` ne survivait en prod)."""
    counts = {"users": 0}
    with _connect() as conn:
        users = conn.execute("SELECT sub, email, name FROM users").fetchall()
    for u in users:
        sub = u["sub"]
        try:
            ensure_personal_org(sub, u.get("email"), u.get("name"))
        except Exception:
            _log.warning("backfill_personal_orgs: ensure échoué %s", sub, exc_info=True)
            continue
        counts["users"] += 1
    return counts

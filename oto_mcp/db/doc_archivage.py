"""Archiver une page, et dater sa dernière vérification (signal #1087).

Deux états d'une page de projet, posés chacun par un GESTE explicite, jamais déduits :

- **`archived_at`** : la page sort de la découverte — `oto_search` et la liste par défaut
  d'`oto_doc` — sans être détruite. Elle reste lisible par son id. L'archivage suit le
  SOUS-ARBRE, comme la suppression (`_SOUS_ARBRE`, borné par `CYCLE`) : une sous-page
  vivante sous une page archivée serait listée sous un parent que la liste ne montre
  pas. Toutes les pages archivées par un même geste portent le MÊME instant (`NOW()`
  d'une seule instruction) : c'est ce qui permet à `désarchiver` de rendre exactement
  ce geste-là, et pas une sous-page archivée avant lui, pour une autre raison.
- **`verified_at` / `verified_by`** : « quelqu'un a relu cette page et l'a jugée encore
  vraie ». Pas `updated_at`, qu'une coquille corrigée fait bouger aussi : une
  vérification est un geste à part, qui ne touche ni le corps, ni `updated_at`, ni
  l'historique de versions.

Les trois colonnes sont posées par la révision `20261024_0900_docs_archivage` ET par le
démarrage, sous garde de catalogue — même DDL.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect
from .projects import _SOUS_ARBRE

COLONNE_ARCHIVEE = "archived_at"
COLONNE_VERIFIEE = "verified_at"
COLONNE_VERIFIEE_PAR = "verified_by"
COLONNES = (COLONNE_ARCHIVEE, COLONNE_VERIFIEE, COLONNE_VERIFIEE_PAR)
# Trois colonnes nullables, sans défaut : catalogue seul, aucune réécriture de `docs`.
DDL_COLONNES = (
    "ALTER TABLE docs "
    f"ADD COLUMN IF NOT EXISTS {COLONNE_ARCHIVEE} TIMESTAMPTZ, "
    f"ADD COLUMN IF NOT EXISTS {COLONNE_VERIFIEE} TIMESTAMPTZ, "
    f"ADD COLUMN IF NOT EXISTS {COLONNE_VERIFIEE_PAR} TEXT")


def archive_doc(doc_id: int) -> tuple[int, Optional[str]]:
    """Archive la page et ses descendants VIVANTS, à un seul instant.

    Rend `(nombre de pages archivées par CE geste, instant)` — `(0, None)` si la page
    l'était déjà (le geste est idempotent : il ne repousse pas la date du premier
    retrait). Un descendant déjà archivé garde sa date."""
    with _connect() as conn:
        rows = conn.execute(
            _SOUS_ARBRE
            + "UPDATE docs SET archived_at = NOW() "
              "WHERE id IN (SELECT id FROM sub) AND archived_at IS NULL "
              "  AND (SELECT archived_at FROM docs WHERE id = %s) IS NULL "
              "RETURNING archived_at",
            (doc_id, doc_id)).fetchall()
    return len(rows), (rows[0]["archived_at"] if rows else None)


def unarchive_doc(doc_id: int) -> int:
    """Désarchive la page et les descendants archivés PAR LE MÊME GESTE (même instant).

    Rend le nombre de pages rendues à la découverte — 0 si la page n'était pas
    archivée. Une sous-page archivée avant sa parente, à un autre instant, le reste :
    la désarchiver est son geste à elle."""
    with _connect() as conn:
        return len(conn.execute(
            _SOUS_ARBRE
            + "UPDATE docs SET archived_at = NULL "
              "WHERE id IN (SELECT id FROM sub) "
              "  AND archived_at = (SELECT archived_at FROM docs WHERE id = %s) "
              "RETURNING id",
            (doc_id, doc_id)).fetchall())


def verify_doc(doc_id: int, sub: Optional[str]) -> Optional[dict]:
    """Pose « vérifiée maintenant, par `sub` ». Ne touche ni le corps, ni `updated_at`,
    ni les révisions. Rend `{verified_at, verified_by}`, ou None si la page n'existe
    plus."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE docs SET verified_at = NOW(), verified_by = %s WHERE id = %s "
            "RETURNING verified_at, verified_by", (sub, doc_id)).fetchone()
    return dict(row) if row else None

"""La SANTÉ des agents d'une org : ce que lit l'onglet Santé (`capabilities/agent_health.py`).

Une lecture par org (`episodes_vivants`, `reparations_recentes`), jamais une par agent :
l'onglet d'une org de deux cents agents coûte deux requêtes.
"""
from __future__ import annotations

from typing import Any

from ._conn import _connect


def episodes_vivants(org_id: int) -> list[dict[str, Any]]:
    """Les épisodes vivants des agents ALLUMÉS de l'org, avec le modèle de l'agent — de
    quoi dire au client ce qui l'attend."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.id, e.org_id, e.trigger_id, e.classe, e.cote, e.statut, e.motif,
                   e.message_client, e.ouvert_at, e.empreinte, t.model AS modele
              FROM agent_episodes e
              JOIN runner_triggers t ON t.id = e.trigger_id AND t.enabled
             WHERE e.org_id = %s AND e.statut <> 'clos'
             ORDER BY e.ouvert_at
            """, (int(org_id),)).fetchall()]


def reparations_recentes(org_id: int, *, jours: int = 30,
                         limit: int = 50) -> list[dict[str, Any]]:
    """Les épisodes de l'org GUÉRIS après un geste du médecin dans les `jours` derniers
    jours, le plus récent d'abord. Avec la note de réparation de l'agent (`repair_note`)
    quand elle date de l'épisode."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.id, e.trigger_id, e.clos_at, e.reparations,
                   CASE WHEN t.repair_note IS NOT NULL
                         AND (t.repair_note->>'at')::timestamptz
                             >= date_trunc('second', e.ouvert_at)
                        THEN t.repair_note END AS repair_note
              FROM agent_episodes e
              LEFT JOIN runner_triggers t ON t.id = e.trigger_id
             WHERE e.org_id = %s AND e.statut = 'clos' AND e.resolution = 'gueri'
               AND jsonb_array_length(e.reparations) > 0
               AND e.clos_at > NOW() - make_interval(days => %s)
             ORDER BY e.clos_at DESC
             LIMIT %s
            """, (int(org_id), int(jours), max(1, min(int(limit), 100)))).fetchall()]

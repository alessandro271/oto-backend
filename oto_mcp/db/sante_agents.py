"""La SANTÉ des agents d'une org : ce que lit l'onglet Santé (`capabilities/agent_health.py`),
le choix de l'org sur la réparation automatique, et l'annulation d'une réparation.

Une lecture par org (`episodes_vivants`, `reparations_recentes`), jamais une par agent :
l'onglet d'une org de deux cents agents coûte deux requêtes. Le choix (`auto_repair`,
`agent_health_settings`) naît COUPÉ : aucune ligne = coupé, un admin de l'org l'allume.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Iterable, Optional

from ._conn import _connect


def auto_repair(org_id: int) -> bool:
    """L'org a-t-elle allumé la réparation automatique ? Aucune ligne = non."""
    with _connect() as conn:
        r = conn.execute("SELECT auto_repair FROM agent_health_settings WHERE org_id = %s",
                         (int(org_id),)).fetchone()
    return bool(r and r["auto_repair"])


def poser_auto_repair(org_id: int, valeur: bool) -> bool:
    with _connect() as conn:
        return bool(conn.execute(
            """
            INSERT INTO agent_health_settings (org_id, auto_repair) VALUES (%s, %s)
            ON CONFLICT (org_id) DO UPDATE
               SET auto_repair = EXCLUDED.auto_repair, updated_at = NOW()
            RETURNING auto_repair
            """, (int(org_id), bool(valeur))).fetchone()["auto_repair"])


def orgs_avec_auto_repair(org_ids: Iterable[int]) -> set[int]:
    """Parmi ces orgs, celles qui ont ALLUMÉ la réparation : une requête par passage."""
    ids = sorted({int(o) for o in org_ids})
    if not ids:
        return set()
    with _connect() as conn:
        return {int(r["org_id"]) for r in conn.execute(
            "SELECT org_id FROM agent_health_settings "
            "WHERE org_id = ANY(%s) AND auto_repair", (ids,)).fetchall()}


def episodes_vivants(org_id: int) -> list[dict[str, Any]]:
    """Les épisodes vivants des agents ALLUMÉS de l'org, avec le modèle de l'agent et la
    copie de leur cause approuvée — de quoi dire au client ce qui l'attend."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.id, e.org_id, e.trigger_id, e.classe, e.cote, e.statut, e.motif,
                   e.message_client, e.ouvert_at, e.empreinte, t.model AS modele,
                   c.copie AS cause_copie, c.cote AS cause_cote
              FROM agent_episodes e
              JOIN runner_triggers t ON t.id = e.trigger_id AND t.enabled
              LEFT JOIN agent_causes c ON c.cle = e.empreinte AND c.statut = 'approuvee'
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


def defaire(episode_id: int, org_id: int, *, acteur: str) -> Optional[dict[str, Any]]:
    """Défait la réparation du médecin sur un épisode — ses ajouts d'outils — en UNE
    transaction : l'épisode puis l'agent sont relus `FOR UPDATE`, modifiés et écrits
    avant que quiconque puisse les relire. Rend `{trigger_id, retires, gardes}`, ou None
    si l'épisode n'est pas de cette org ou n'a plus rien à défaire (deux clics : le
    second attend le verrou, relit l'annulation du premier, et ne fait rien).

    Un outil n'est RETIRÉ des outils de l'agent que s'il y est encore ET que l'agent n'a
    été écrit par personne depuis le dernier ajout du médecin : son `updated_at` est
    encore, à la microseconde, celui que cet ajout lui a donné (`maj_at`,
    `episodes_agents.horodater_l_ajout`), et `updated_by` encore son auteur.
    Une autre écriture depuis (une personne a retouché l'agent, peut-être retiré puis
    remis l'outil) : l'outil présent n'est plus sûrement celui du médecin, il est GARDÉ
    et dit (`gardes`). Dans tous les cas, l'annulation s'écrit sur l'épisode, le médecin
    ne redonne plus ces outils à l'agent avant `OUTILS_ANNULES_DUREE_JOURS`, et la note
    de réparation de l'agent s'efface si elle date de cet épisode."""
    from . import episodes_agents, runner_triggers as rt
    with _connect() as conn:
        ep = conn.execute(
            "SELECT id, org_id, trigger_id, reparations, ouvert_at, clos_at "
            "FROM agent_episodes WHERE id = %s AND org_id = %s FOR UPDATE",
            (int(episode_id), int(org_id))).fetchone()
        if ep is None:
            return None
        outils = episodes_agents.outils_a_defaire(ep["reparations"])
        if not outils:
            return None
        tid = int(ep["trigger_id"])
        maj, auteur = episodes_agents.derniere_ecriture_du_medecin(ep["reparations"])
        t = conn.execute(
            "SELECT tools, (%(maj)s::timestamptz IS NOT NULL "
            "               AND updated_at = %(maj)s::timestamptz "
            "               AND updated_by IS NOT DISTINCT FROM %(auteur)s) AS intact "
            "FROM runner_triggers WHERE id = %(id)s AND org_id = %(org)s FOR UPDATE",
            {"maj": maj, "auteur": auteur, "id": tid, "org": int(org_id)}).fetchone()
        presents = list((t or {}).get("tools") or [])
        intact = bool(t and t["intact"])
        retires = [o for o in outils if o in presents and intact]
        gardes = [o for o in outils if o in presents and not intact]
        if t is not None:
            conn.execute(
                f"""
                UPDATE runner_triggers
                   SET {rt.COLONNE_OUTILS_ANNULES} =
                         COALESCE({rt.COLONNE_OUTILS_ANNULES}, '{{}}'::jsonb)
                         || (SELECT jsonb_object_agg(o, to_char(
                                      (NOW() + %(duree)s) AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
                               FROM unnest(%(outils)s::text[]) o)
                 WHERE id = %(id)s AND org_id = %(org)s
                """, {"duree": timedelta(days=rt.OUTILS_ANNULES_DUREE_JOURS),
                      "outils": outils, "id": tid, "org": int(org_id)})
            if retires:
                conn.execute(
                    f"UPDATE runner_triggers SET tools = %s::jsonb, {rt._CHANGEMENT_PAR} "
                    "WHERE id = %s AND org_id = %s",
                    (json.dumps([o for o in presents if o not in retires],
                                ensure_ascii=False), acteur, tid, int(org_id)))
            rt.effacer_reparation_pour_le_client(conn, tid, int(org_id),
                                                 depuis=ep["ouvert_at"],
                                                 jusqu_a=ep["clos_at"])
        geste = {"action": episodes_agents.ANNULATION, "acteur": acteur, "outils": outils,
                 "retires": retires, "gardes": gardes,
                 "detail": ", ".join(f"- {o}" for o in retires)[:500] or None}
        conn.execute(
            """
            UPDATE agent_episodes
               SET reparations = reparations || jsonb_build_array(
                       %s::jsonb || jsonb_build_object(
                           'at', to_char(NOW() AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SSZ')))
             WHERE id = %s
            """, (json.dumps(geste, ensure_ascii=False), int(ep["id"])))
    return {"trigger_id": tid, "outils": outils, "retires": retires, "gardes": gardes}

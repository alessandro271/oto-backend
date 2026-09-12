"""« Dernières modifications » — pages et procédures, fusionnées par `updated_at`.

Une lecture DÉRIVÉE (oto#191) : aucune table, aucun journal — les colonnes
`updated_at` que les deux stores tiennent déjà, lues en UNE requête (`UNION ALL`
+ `ORDER BY … LIMIT`), pour que le tri et la coupe se fassent en base et non
après avoir rapatrié deux listes entières.

Le PÉRIMÈTRE n'est pas calculé ici : l'appelant passe les projets lisibles
(`ownership.accessible_project_ids`) et les propriétaires de procédures à portée
(`ownership.project_scope_owners` + la personne). Même règle que la recherche
(`db/search.py`) — « cherchable ⇔ lisible » devient « listé ⇔ lisible ».

⚠️ **L'auteur n'est jamais déduit.** Ni `docs` ni `org_instructions` ne portent
« qui a fait la dernière modification » : `docs` n'a que `created_by`,
`org_instructions.set_by` survit à un déplacement (`instruction_ownership.move`)
qui touche `updated_at` sans le réécrire. La donnée existe pourtant, dans les
tables de révisions — et elle s'apparie EXACTEMENT : la révision est insérée dans
la même transaction que l'`UPDATE`, donc `NOW()` y vaut la même valeur, à la
microseconde. La jointure est une égalité de timestamps, pas une fenêtre. Quand
rien ne s'apparie (page déplacée, procédure transférée, révision sans auteur),
l'auteur est `NULL` — l'écran affiche une date sans nom, jamais un nom faux.
Une page jamais modifiée depuis sa création (`updated_at = created_at`, même
transaction d'`INSERT`) a pour auteur son créateur : c'est la modification.

L'auteur se cherche APRÈS la coupe : la sous-requête `m` trie et coupe sur les seules
colonnes de tri, et l'appariement aux révisions ne tourne que sur les `limit` lignes
gardées — pas sur chaque page des projets lisibles, pour un îlot chargé à chaque accueil.
"""
from __future__ import annotations

from ._conn import _connect

# Les deux branches rendent les MÊMES colonnes, dans le même ordre : c'est la
# condition du `UNION ALL`, et ce que l'appariement de l'auteur lit ensuite.
_DOCS = """
SELECT 'doc' AS type, d.id AS id, d.title AS title,
       d.project_id AS project_id, p.name AS project_name,
       NULL::text AS slug, NULL::text AS owner_type, NULL::text AS owner_id,
       NULL::integer AS version, d.created_by AS created_by,
       d.created_at AS created_at, d.updated_at AS updated_at
FROM docs d
JOIN projects p ON p.id = d.project_id
WHERE d.project_id = ANY(%s)
"""

# `slug <> 'claude_md'` et `archived_at IS NULL` : les deux prédicats des lectures du
# store (`list_instructions`, `list_instructions_for_owners`) — une archivée « cesse
# d'être proposée », et le readme pré-0042 n'est pas une procédure.
_PROCEDURES = """
SELECT 'procedure' AS type, oi.id AS id, oi.title AS title,
       NULL::bigint AS project_id, NULL::text AS project_name,
       oi.slug AS slug, oi.owner_type AS owner_type, oi.owner_id AS owner_id,
       oi.version AS version, NULL::text AS created_by,
       oi.created_at AS created_at, oi.updated_at AS updated_at
FROM org_instructions oi
JOIN unnest(%s::text[], %s::text[]) AS o(t, i)
  ON oi.owner_type = o.t AND oi.owner_id = o.i
WHERE oi.slug <> %s AND oi.archived_at IS NULL
"""

_AUTEUR = """
CASE WHEN m.type = 'doc' THEN
       CASE WHEN m.updated_at = m.created_at THEN m.created_by
            ELSE (SELECT r.edited_by FROM doc_revisions r
                  WHERE r.doc_id = m.id AND r.created_at = m.updated_at
                  ORDER BY r.id DESC LIMIT 1)
       END
     ELSE (SELECT r.set_by FROM org_instruction_revisions r
           WHERE r.owner_type = m.owner_type AND r.owner_id = m.owner_id
             AND r.slug = m.slug AND r.version = m.version
             AND r.created_at = m.updated_at
           LIMIT 1)
END
"""

_ORDRE = "m.updated_at DESC, m.type, m.id DESC"


def recent_changes(project_ids: list[int], procedure_owners: list[tuple[str, str]],
                   *, limit: int, base_slug: str) -> list[dict]:
    """Les `limit` dernières modifications parmi les pages de `project_ids` et les
    procédures possédées par `procedure_owners`, les plus récentes d'abord.

    Deux périmètres vides = aucune requête : la liste vide est une réponse, pas un
    parcours. Un seul des deux vide = sa branche ne rend rien (`ANY('{}')`,
    `unnest` de tableaux vides), sans cas particulier."""
    if not project_ids and not procedure_owners:
        return []
    sql = (
        "SELECT m.type, m.id, m.title, m.project_id, m.project_name, m.slug, "
        f"m.owner_type, m.updated_at, {_AUTEUR} AS author_sub "
        f"FROM (SELECT * FROM (({_DOCS}) UNION ALL ({_PROCEDURES})) AS m "
        f"ORDER BY {_ORDRE} LIMIT %s) AS m "
        f"ORDER BY {_ORDRE}"
    )
    params = (
        [int(p) for p in project_ids],
        [t for t, _ in procedure_owners], [i for _, i in procedure_owners],
        base_slug, int(limit),
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]

"""Les lecteurs View d'une app qui cliquaient — `oto-mcp maintenance apps-lecteurs-actifs`.

Depuis le rôle Use (`contributor`, `capabilities/apps_appel.py`), un partage d'app
`viewer` ouvre l'app sans en cliquer les boutons. Avant lui, tout lecteur cliquait. Ce
module trouve les partages `viewer` d'app par lesquels quelqu'un a fait un GESTE
(`app_consents` brûlé : écrire, ajouter, lancer, déposer) dans la fenêtre, et sans
lesquels il ne cliquerait plus — puis, avec `appliquer=True`, les passe en `contributor`.

- **Qui compte** : la personne d'un consentement brûlé (`used_at`) dans les `jours`
  derniers jours, sur une app non archivée, qui ne cliquera pas autrement : ni
  propriétaire de l'app (la personne, un membre de l'org ou de l'équipe propriétaire,
  un admin de l'org de cette équipe), ni titulaire d'un partage vivant `contributor`,
  `editor` ou `manager` sur l'app, par elle-même, une de ses équipes ou une de ses orgs.
- **Ce qui se promeut** : chaque partage vivant `viewer` de l'app qui atteint cette
  personne — nominatif, d'équipe ou d'org. Un partage d'org ou d'équipe promu fait
  cliquer tous ses membres, comme avant le rôle Use.
- **À blanc par défaut** : une transaction en lecture seule. Le rapport ne porte que des
  identifiants (app, type et id du principal) et des comptes, jamais un nom ni un
  contenu.
- `appliquer=True` : UNE transaction bornée (`statement_timeout` 15 s, `lock_timeout`
  5 s) qui relit la même sélection et promeut ces partages (`role = 'contributor'`,
  `permission` reste `read`). Idempotente : rejouée, la sélection est vide.
- Refuse d'appliquer tant que la base n'admet pas `contributor`
  (`resource_grants_role_check`, révision `20261119_1300_app_use_role`).
"""
from __future__ import annotations

import logging

from .db._partage_vivant import PARTAGE_VIVANT_G

logger = logging.getLogger(__name__)

JOURS_DEFAUT = 30
_BORNES = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")


def _principal_de(sub_col: str) -> str:
    """Le partage `g` atteint-il la personne `sub_col` : elle-même, une de ses orgs, une
    de ses équipes."""
    return (f"((g.principal_type = 'user' AND g.principal_id = {sub_col}) "
            f"OR (g.principal_type = 'org' AND EXISTS (SELECT 1 FROM org_members m "
            f"    WHERE m.org_id::text = g.principal_id AND m.sub = {sub_col})) "
            f"OR (g.principal_type = 'group' AND EXISTS (SELECT 1 FROM org_group_members gm "
            f"    WHERE gm.group_id::text = g.principal_id AND gm.sub = {sub_col})))")


def selection_sql() -> str:
    """La sélection, une seule fois : l'audit à blanc et la promotion la lisent toutes
    deux. Partages VIVANTS seuls (`PARTAGE_VIVANT_G`). Paramètre : `jours`."""
    atteint_le_clic = _principal_de("cl.sub")
    atteint_le_lecteur = _principal_de("x.sub")
    return f"""
WITH clics AS (
  SELECT c.app_id, c.sub, count(*) AS gestes
  FROM app_consents c
  WHERE c.used_at IS NOT NULL AND c.used_at > now() - make_interval(days => %(jours)s)
  GROUP BY c.app_id, c.sub),
sans_clic AS (
  SELECT cl.app_id, cl.sub, cl.gestes
  FROM clics cl JOIN apps a ON a.id = cl.app_id AND a.archived_at IS NULL
  WHERE NOT (
       (a.owner_type = 'user' AND a.owner_id = cl.sub)
    OR (a.owner_type = 'org' AND EXISTS (SELECT 1 FROM org_members m
          WHERE m.org_id::text = a.owner_id AND m.sub = cl.sub))
    OR (a.owner_type = 'group' AND EXISTS (SELECT 1 FROM org_group_members gm
          WHERE gm.group_id::text = a.owner_id AND gm.sub = cl.sub))
    OR (a.owner_type = 'group' AND EXISTS (SELECT 1 FROM org_groups og
          JOIN org_members m ON m.org_id = og.org_id AND m.org_role = 'org_admin'
          WHERE og.id::text = a.owner_id AND m.sub = cl.sub))
    OR EXISTS (SELECT 1 FROM resource_grants g
          WHERE g.resource_type = 'app' AND g.resource_id = cl.app_id::text
            AND g.role IN ('contributor', 'editor', 'manager') AND {PARTAGE_VIVANT_G}
            AND {atteint_le_clic})))
SELECT g.resource_id AS app_id, g.principal_type, g.principal_id,
       count(DISTINCT x.sub) AS personnes, sum(x.gestes)::bigint AS gestes
FROM sans_clic x
JOIN resource_grants g ON g.resource_type = 'app' AND g.resource_id = x.app_id::text
  AND g.role = 'viewer' AND {PARTAGE_VIVANT_G} AND {atteint_le_lecteur}
GROUP BY g.resource_id, g.principal_type, g.principal_id
ORDER BY g.resource_id::bigint, g.principal_type, g.principal_id
"""

_PROMOUVOIR = ("UPDATE resource_grants SET role = 'contributor' "
               "WHERE resource_type = 'app' AND resource_id = %s AND principal_type = %s "
               "AND principal_id = %s AND role = 'viewer'")


class BaseSansRoleUse(RuntimeError):
    """La base refuse encore `contributor` : la révision du rôle Use n'est pas jouée."""


def _borner(conn) -> None:
    for sql in _BORNES:
        conn.execute(sql)


def _admet_contributor(conn) -> bool:
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conname = 'resource_grants_role_check' "
        "AND conrelid = 'resource_grants'::regclass").fetchone()
    return row is not None and "'contributor'" in row["d"]


def _selection(conn, jours: int) -> list[dict]:
    return [{"app_id": int(r["app_id"]), "principal_type": r["principal_type"],
             "principal_id": str(r["principal_id"]), "personnes": int(r["personnes"]),
             "gestes": int(r["gestes"])}
            for r in conn.execute(selection_sql(), {"jours": int(jours)}).fetchall()]


def passer(*, appliquer: bool, jours: int = JOURS_DEFAUT) -> dict:
    """Le rapport : `{"applique", "jours", "partages": [{app_id, principal_type,
    principal_id, personnes, gestes}], "promus"}`. Lève `BaseSansRoleUse` si
    `appliquer` sur une base qui n'admet pas encore `contributor`."""
    from .db import _connect

    if jours < 1:
        raise ValueError("la fenêtre compte au moins un jour")
    with _connect() as conn:
        if not appliquer:
            conn.execute("SET TRANSACTION READ ONLY")
        _borner(conn)
        if not appliquer:
            return {"applique": False, "jours": jours,
                    "partages": _selection(conn, jours), "promus": 0}
        if not _admet_contributor(conn):
            raise BaseSansRoleUse(
                "resource_grants n'admet pas encore `contributor` : jouer d'abord "
                "`oto-mcp migrer upgrade head` (révision 20261119_1300_app_use_role)")
        partages = _selection(conn, jours)
        promus = 0
        for p in partages:
            promus += conn.execute(_PROMOUVOIR, (str(p["app_id"]), p["principal_type"],
                                                 p["principal_id"])).rowcount
    logger.info("apps-lecteurs-actifs : %s partage(s) viewer promu(s) en contributor", promus)
    return {"applique": True, "jours": jours, "partages": partages, "promus": promus}


def reste(rapport: dict) -> bool:
    """À blanc : il y a des partages à promouvoir. Appliqué : tout n'a pas été promu."""
    if rapport["applique"]:
        return rapport["promus"] < len(rapport["partages"])
    return bool(rapport["partages"])


def lignes_du_rapport(rapport: dict) -> list[str]:
    """Le rapport imprimé : des identifiants et des comptes, jamais un nom."""
    mode = ("APPLIQUÉ" if rapport["applique"]
            else "À BLANC (rien n'est écrit ; --appliquer)")
    out = [f"apps-lecteurs-actifs : {mode} — gestes des {rapport['jours']} derniers jours"]
    verbe = "promu en contributor" if rapport["applique"] else "à promouvoir"
    for p in rapport["partages"]:
        out.append(f"  app #{p['app_id']} {p['principal_type']}:{p['principal_id']} "
                   f"({verbe}) : {p['personnes']} personne(s), {p['gestes']} geste(s)")
    if not rapport["partages"]:
        out.append("  aucun partage viewer par lequel quelqu'un cliquait")
    else:
        apps = len({p["app_id"] for p in rapport["partages"]})
        out.append(f"  total : {len(rapport['partages'])} partage(s) sur {apps} app(s)"
                   + (f", {rapport['promus']} promu(s)" if rapport["applique"] else ""))
    return out

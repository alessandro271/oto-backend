"""Les listes d'outils enregistrées suivent la séparation lire / agir (`<ns>` / `<ns>_act`).

Le 17/10/2026, `gmail_message`, `gmail_compose`, `gmail_list_accounts`, `outlook_message`
et `outlook_compose` sont retirés au profit de `gmail` / `gmail_act` et `outlook` /
`outlook_act` (`outils_retires.RENOMMAGES_ACT`, LA table de correspondance — cette
révision la lit, elle ne la recopie pas). Les listes d'outils que des personnes ont
enregistrées citent les anciens noms ; chacune GARDE SON SENS :

- une liste qui AUTORISE (`projects.mcp_tools`, `runner_fleets.tools`,
  `runner_triggers.tools`, `user_enabled_tools`, et la liste `payload.tools` d'un travail
  du runner pas encore terminé — celle que le serveur tient à chaque appel depuis #1256,
  `outils_du_travail`) remplace l'ancien nom par TOUS ceux qui reprennent ce qu'il faisait
  (`outils_retires.remplacants`) : un agent qui pouvait envoyer par `gmail_compose` peut
  envoyer par `gmail_act` ;
- une liste qui MASQUE (`user_disabled_tools`, `org_disabled_tools`,
  `group_disabled_tools`) masque de même tous les remplaçants : ce qui était caché le
  reste.

Le journal des appels (`tool_calls`, totaux du jour) et les travaux TERMINÉS (`done`,
`failed`, `expired`) ne sont PAS réécrits : c'est l'histoire, elle porte le nom servi ce
jour-là. Un travail déjà réservé (`claimed`) est réécrit en base — le serveur le juge sur
cette liste — mais son worker garde la copie qu'il a reçue : ce run-là ne voit pas les
nouveaux noms ; le suivant, si.

**Verrous.** Des `UPDATE`/`INSERT`/`DELETE` par clé primaire sur les seules lignes qui
citent un ancien nom (quelques-unes) ; `lock_timeout` 5 s.

**Ordre avec le code : APRÈS le déploiement, ou juste avant — sans fenêtre utile entre
les deux.** L'ancien code ne connaît pas les nouveaux noms : une liste réécrite avant le
déploiement ne retrouve pas ses outils jusqu'au déploiement ; le nouveau code refuse les
anciens noms : une liste non réécrite après le déploiement ne les retrouve pas non plus.
Prod et préprod partagent la base : la jouer au tag de prod.

Retour arrière : aucun. Les anciens noms sont la faille corrigée ; revenir au code
précédent demande de réécrire les listes à la main (le nouveau nom → l'ancien), pas de
rendre aux anciens noms leurs droits.

Révision : 0062_outils_act
Précédente : 0061_valeurs_materialisees
"""
from __future__ import annotations

import json
import logging

from alembic import context, op

revision = "0062_outils_act"
down_revision = "0061_valeurs_materialisees"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")

#: Les listes d'outils, par (table, clé primaire, colonne, type).
_LISTES = (
    ("projects", "id", "mcp_tools", "text[]"),
    ("runner_fleets", "id", "tools", "jsonb"),
    ("runner_triggers", "id", "tools", "jsonb"),
)
#: Les tables à une ligne par outil : (table, colonnes de la clé hors `tool_name`).
_LIGNES = (
    ("user_enabled_tools", ("sub", "org_id")),
    ("user_disabled_tools", ("sub", "org_id")),
    ("org_disabled_tools", ("org_id",)),
    ("group_disabled_tools", ("group_id",)),
)


def reecrire(outils: list, correspondance: dict[str, tuple[str, ...]]) -> list[str]:
    """La liste `outils`, chaque ancien nom remplacé SUR PLACE par ses remplaçants, sans
    doublon et dans l'ordre. Un élément qui n'est pas un nom d'outil est une liste que
    cette révision ne sait pas lire : refus, jamais une réécriture approximative."""
    out: list[str] = []
    for nom in outils:
        if not isinstance(nom, str):
            raise ValueError(f"liste d'outils illisible : élément {nom!r}")
        for n in correspondance.get(nom, (nom,)):
            if n not in out:
                out.append(n)
    return out


def correspondance() -> dict[str, tuple[str, ...]]:
    from oto_mcp import outils_retires
    return {r.ancien: outils_retires.remplacants(r.ancien)
            for r in outils_retires.RENOMMAGES_ACT}


#: Les travaux du runner dont la liste d'outils compte encore (`runner_jobs.status`).
_TRAVAUX_VIVANTS = ("pending", "held", "claimed")


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire : la réécriture dépend des
        # listes en place, elle se lit au journal de la vraie montée.
        return
    op.execute(_ATTENTE_MAX)
    table = correspondance()
    anciens = sorted(table)
    conn = op.get_bind()
    for nom_table, cle, colonne, genre in _LISTES:
        filtre = (f"{colonne} && %(anciens)s::text[]" if genre == "text[]"
                  else f"{colonne} ?| %(anciens)s::text[]")
        lignes = conn.exec_driver_sql(
            f"SELECT {cle}, {colonne} FROM {nom_table} WHERE {filtre}",
            {"anciens": anciens}).all()
        for pk, outils in lignes:
            nouveaux = reecrire(list(outils), table)
            valeur = (nouveaux if genre == "text[]" else json.dumps(nouveaux))
            conn.exec_driver_sql(
                f"UPDATE {nom_table} SET {colonne} = %(v)s::{genre} WHERE {cle} = %(pk)s",
                {"v": valeur, "pk": pk})
        logger.info("0062 : %s.%s — %d liste(s) réécrite(s)", nom_table, colonne,
                    len(lignes))
    travaux = conn.exec_driver_sql(
        "SELECT id, payload->'tools' FROM runner_jobs WHERE status = ANY(%(vivants)s) "
        "AND jsonb_typeof(payload->'tools') = 'array' "
        "AND payload->'tools' ?| %(anciens)s::text[]",
        {"vivants": list(_TRAVAUX_VIVANTS), "anciens": anciens}).all()
    for pk, outils in travaux:
        conn.exec_driver_sql(
            "UPDATE runner_jobs SET payload = jsonb_set(payload, '{tools}', %(v)s::jsonb) "
            "WHERE id = %(pk)s",
            {"v": json.dumps(reecrire(list(outils), table)), "pk": pk})
    logger.info("0062 : runner_jobs.payload.tools — %d travail(s) vivant(s) réécrit(s)",
                len(travaux))
    for nom_table, cle in _LIGNES:
        autres = ", ".join(cle + _autres(nom_table))
        n = 0
        for ancien in anciens:
            for nouveau in table[ancien]:
                n += conn.exec_driver_sql(
                    f"INSERT INTO {nom_table} (tool_name, {autres}) "
                    f"SELECT %(nouveau)s, {autres} FROM {nom_table} "
                    f"WHERE tool_name = %(ancien)s ON CONFLICT DO NOTHING",
                    {"ancien": ancien, "nouveau": nouveau}).rowcount
            conn.exec_driver_sql(f"DELETE FROM {nom_table} WHERE tool_name = %(ancien)s",
                                 {"ancien": ancien})
        logger.info("0062 : %s — %d ligne(s) posée(s) sous les nouveaux noms", nom_table, n)


def _autres(nom_table: str) -> tuple[str, ...]:
    """Les colonnes d'une table à une ligne par outil hors clé et `tool_name`
    (`db/schema/visibility.py`), recopiées telles quelles sur la ligne du nouveau nom."""
    return {"user_enabled_tools": ("enabled_at",),
            "user_disabled_tools": ("disabled_at",),
            "org_disabled_tools": ("disabled_by", "disabled_at"),
            "group_disabled_tools": ("disabled_by", "disabled_at")}[nom_table]


def downgrade() -> None:
    raise NotImplementedError(
        "0062 n'a pas de retour arrière : les anciens noms d'outils sont la faille "
        "corrigée (cf. docstring).")

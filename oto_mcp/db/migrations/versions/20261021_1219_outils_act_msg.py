"""Les listes d'outils enregistrées suivent la séparation lire / agir des messageries.

Le 21/10/2026, les lecteurs de messagerie et leurs outils d'envoi deviennent des paires
(`whatsapp`/`whatsapp_act`, `telegram`/`telegram_act`, `instagram`/`instagram_act`,
`linkedin_unipile`/`linkedin_unipile_act`, `teams`/`teams_act`, `chat`/`chat_act`) : les
anciens noms (`whatsapp_chat`, `whatsapp_send`, `linkedin_unipile_chat`, `teams_message`,
`chat_send`…) sont retirés. Cette révision lit `outils_retires.RENOMMAGES_MESSAGERIES`,
la table de ce lot, sans la recopier.

**Le sens, décision d'Alexis du 21/10/2026 : autoriser reste étroit, masquer reste large.**

- Une liste qui AUTORISE (`projects.mcp_tools`, `runner_fleets.tools`,
  `runner_triggers.tools`, `user_enabled_tools`, la liste `payload.tools` d'un travail du
  runner pas encore terminé) reçoit `outils_retires.remplacants_autorises` : un ancien
  outil MIXTE ne donne que le nouveau lecteur — `whatsapp_chat` devient `whatsapp`, JAMAIS
  `whatsapp_act`, bien que l'ancien lecteur envoyât encore en transition (c'est la faille
  corrigée) ; un ancien outil d'envoi dédié (`whatsapp_send`, `teams_send`…) donne
  `<ns>_act` seulement si celui-ci ne fait rien de plus que lui — depuis le lot des
  envois, `linkedin_unipile_send` ne donne rien (`linkedin_unipile_act` publie et
  invite aussi).
- Une liste qui MASQUE (`user_disabled_tools`, `org_disabled_tools`,
  `group_disabled_tools`) reçoit `outils_retires.remplacants` : tout ce que l'ancien outil
  faisait reste caché — `whatsapp_chat` masqué masque `whatsapp` ET `whatsapp_act`.

Le journal des appels et les travaux TERMINÉS ne sont pas réécrits : c'est l'histoire. Un
travail réservé (`claimed`) est réécrit en base, son worker garde sa copie (cf. 0062).
La mécanique (tables, réécriture sur place sans doublon, refus d'une liste illisible) est
celle de 0062, chargée telle quelle ; seules les deux correspondances diffèrent.
`reecrire_les_listes` sert aussi aux lots suivants.

**Verrous.** `UPDATE`/`INSERT`/`DELETE` par clé primaire sur les seules lignes qui citent
un ancien nom ; `lock_timeout` 5 s.

**Ordre avec le code : APRÈS le déploiement, ou juste avant — sans fenêtre utile entre
les deux** (cf. 0062). Prod et préprod partagent la base : la jouer au tag de prod.

Retour arrière : aucun, comme 0062 — les anciens noms sont la faille corrigée.

Révision : 20261021_1219_outils_act_msg
Précédente : 0072_triggers_hook_sources
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

from alembic import context, op

revision = "20261021_1219_outils_act_msg"
down_revision = "0072_triggers_hook_sources"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")


def _revision_0062():
    """La révision 0062, pour sa mécanique (`reecrire`, `_LISTES`, `_LIGNES`, `_autres`,
    `_TRAVAUX_VIVANTS`) — jamais pour sa correspondance, figée sur le lot du 17/10."""
    chemin = Path(__file__).with_name("20261017_0062_outils_act.py")
    spec = importlib.util.spec_from_file_location("revision_0062_outils_act", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def correspondances(table) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """`(autorise, masque)` des anciens noms de `table` (un lot de `outils_retires`)."""
    from oto_mcp import outils_retires
    anciens = dict.fromkeys(r.ancien for r in table)
    return ({a: outils_retires.remplacants_autorises(a) for a in anciens},
            {a: outils_retires.remplacants(a) for a in anciens})


#: Les tables à une ligne par outil qui AUTORISENT ; les autres de 0062 masquent.
_LIGNES_QUI_AUTORISENT = frozenset({"user_enabled_tools"})


def reecrire_les_listes(conn, autorise: dict, masque: dict, etiquette: str) -> None:
    """Réécrit les listes enregistrées : `autorise` pour celles qui autorisent, `masque`
    pour celles qui masquent (mêmes anciens noms dans les deux). Un nom qui figure parmi
    ses propres remplaçants est un outil qui RESTE servi (lot des envois : il perd ses
    ops d'envoi) — il garde sa place, on n'ajoute que les autres."""
    m = _revision_0062()
    anciens = sorted(autorise)
    for nom_table, cle, colonne, genre in m._LISTES:
        filtre = (f"{colonne} && %(anciens)s::text[]" if genre == "text[]"
                  else f"{colonne} ?| %(anciens)s::text[]")
        lignes = conn.exec_driver_sql(
            f"SELECT {cle}, {colonne} FROM {nom_table} WHERE {filtre}",
            {"anciens": anciens}).all()
        for pk, outils in lignes:
            nouveaux = m.reecrire(list(outils), autorise)
            if nouveaux == list(outils):
                continue
            valeur = nouveaux if genre == "text[]" else json.dumps(nouveaux)
            conn.exec_driver_sql(
                f"UPDATE {nom_table} SET {colonne} = %(v)s::{genre} WHERE {cle} = %(pk)s",
                {"v": valeur, "pk": pk})
        logger.info("%s : %s.%s — %d liste(s) réécrite(s)", etiquette, nom_table, colonne,
                    len(lignes))
    travaux = conn.exec_driver_sql(
        "SELECT id, payload->'tools' FROM runner_jobs WHERE status = ANY(%(vivants)s) "
        "AND jsonb_typeof(payload->'tools') = 'array' "
        "AND payload->'tools' ?| %(anciens)s::text[]",
        {"vivants": list(m._TRAVAUX_VIVANTS), "anciens": anciens}).all()
    for pk, outils in travaux:
        if m.reecrire(list(outils), autorise) == list(outils):
            continue
        conn.exec_driver_sql(
            "UPDATE runner_jobs SET payload = jsonb_set(payload, '{tools}', %(v)s::jsonb) "
            "WHERE id = %(pk)s",
            {"v": json.dumps(m.reecrire(list(outils), autorise)), "pk": pk})
    logger.info("%s : runner_jobs.payload.tools — %d travail(s) vivant(s) réécrit(s)",
                etiquette, len(travaux))
    for nom_table, cle in m._LIGNES:
        sens = autorise if nom_table in _LIGNES_QUI_AUTORISENT else masque
        autres = ", ".join(cle + m._autres(nom_table))
        n = 0
        for ancien in anciens:
            for nouveau in sens[ancien]:
                if nouveau == ancien:
                    continue
                n += conn.exec_driver_sql(
                    f"INSERT INTO {nom_table} (tool_name, {autres}) "
                    f"SELECT %(nouveau)s, {autres} FROM {nom_table} "
                    f"WHERE tool_name = %(ancien)s ON CONFLICT DO NOTHING",
                    {"ancien": ancien, "nouveau": nouveau}).rowcount
            if ancien not in sens[ancien]:
                conn.exec_driver_sql(f"DELETE FROM {nom_table} WHERE tool_name = %(ancien)s",
                                     {"ancien": ancien})
        logger.info("%s : %s — %d ligne(s) posée(s) sous les nouveaux noms", etiquette,
                    nom_table, n)


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire (cf. 0062).
        return
    from oto_mcp import outils_retires

    op.execute(_ATTENTE_MAX)
    reecrire_les_listes(op.get_bind(),
                        *correspondances(outils_retires.RENOMMAGES_MESSAGERIES), revision)


def downgrade() -> None:
    raise NotImplementedError(
        f"{revision} n'a pas de retour arrière : les anciens noms d'outils sont la faille "
        "corrigée (cf. docstring).")

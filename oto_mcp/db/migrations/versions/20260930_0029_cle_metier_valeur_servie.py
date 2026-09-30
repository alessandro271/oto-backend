"""cle_metier_valeur_servie : les index de clé métier comparent la valeur que sert la lecture.

oto#223. Les index `ds_bkey_<ns>` étaient construits sur le texte V1
(`COALESCE(data->'k'->>'valeur', data->>'k')`) ; le code du même lot les pose, les
cherche, et compte leurs doublons sur `bkey_index_expr`, qui est désormais la règle de
lecture (`db/paths.py::field_value_sql`). Cette révision RECONSTRUIT chaque index de
clé existant sur la nouvelle expression, par `datastore_ensure_key_index` — le geste
applicatif, tel quel : `CREATE UNIQUE INDEX CONCURRENTLY ds_bkey_<ns>_v2 … WHERE ns_id
= <ns> AND <expr> IS NOT NULL`, puis `DROP INDEX ds_bkey_<ns>` et `ALTER INDEX … RENAME`.
Créer avant de déposer : aucune fenêtre sans unicité.

**CONCURRENTLY** : n'arrête pas les écritures pendant la construction, mais attend la
fin de toute transaction ouverte avant lui ; hors transaction, d'où `autocommit_block`.
Sans borne (`bornee=False`) : c'est un geste d'exploitation, attendre son tour ne
dessert personne. Le `DROP INDEX` de l'ancien prend un verrou exclusif bref.

**Doublons** : sur une case qui sert une valeur, V1 et la nouvelle règle rendent le
même texte ; elles ne diffèrent que sur une case qui n'en sert aucune (couches seules,
`{"valeur": null, …}`), que la nouvelle expression rend NULL, donc laisse HORS de
l'index partiel. L'ensemble indexé est donc un sous-ensemble de celui de V1, aux mêmes
valeurs : un index V1 valide garantit que sa reconstruction ne rencontre aucun doublon.
Seul un index V1 INVALIDE (pose interrompue, laissée derrière) pourrait couvrir des
doublons — la reconstruction de CE tableau échoue alors, les autres continuent, et la
révision lève en nommant les tableaux en échec (rien n'est estampillé ; la rejouer
saute ceux déjà reconstruits).

Seuls les tableaux qui déclarent une clé ET portent déjà leur index sont visés : un
index manquant est l'affaire de `oto-mcp maintenance key-indexes`, qui le pose sur la
nouvelle expression. Un index orphelin (sans clé déclarée) n'est pas touché.

Ordre avec le tag : indifférent, jamais cassant. Code neuf + index V1 → le lookup ne
sert plus l'index (parcours séquentiel, plus lent, juste) ; index neufs + ancien code →
même chose dans l'autre sens. À jouer juste après la fusion pour refermer la fenêtre.

Le retour arrière reconstruit les index sur V1 ; il peut échouer sur un tableau où deux
lignes portent la même enveloppe sans valeur (V1 les compare, la règle non) — même
levée nommée.

Révision : 0029_cle_metier_valeur_servie
Précédente : 0028_partage_en_attente
"""
from __future__ import annotations

from alembic import op
from psycopg import sql
from sqlalchemy import text

revision = "0029_cle_metier_valeur_servie"
down_revision = "0028_partage_en_attente"
branch_labels = None
depends_on = None

#: Le texte V1, tel qu'il était posé avant oto#223 — la cible du retour arrière.
_V1 = "COALESCE(data->{k}->>'valeur', data->>{k})"

#: Ce que la règle de lecture porte et que V1 ne porte pas : de quoi reconnaître un
#: index déjà reconstruit, pour que rejouer la révision ne refasse que le reste.
_MARQUE_REGLE = "jsonb_typeof"

_INDEX_DE_CLE = text(
    "SELECT d.id, d.schema->>'key' AS cle, pg_get_indexdef(i.indexrelid) AS def "
    "FROM user_datastores d "
    "JOIN pg_class c ON c.relname = 'ds_bkey_' || d.id "
    "JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE d.schema->>'key' IS NOT NULL AND d.schema->>'key' <> '' ORDER BY d.id")


def _index_de_cle() -> list:
    return list(op.get_bind().execute(_INDEX_DE_CLE).mappings())


def _lever_si_echecs(echecs: list, sens: str) -> None:
    if echecs:
        raise RuntimeError(
            f"{sens} : {len(echecs)} index de clé métier non reconstruits — "
            + " ; ".join(f"tableau {ns} : {e}" for ns, e in echecs)
            + ". Les autres le sont ; rejouer la révision ne refait que ceux-là.")


def upgrade() -> None:
    from oto_mcp.db.datastore import datastore_ensure_key_index
    echecs: list = []
    with op.get_context().autocommit_block():
        for r in _index_de_cle():
            if _MARQUE_REGLE in r["def"]:
                continue
            try:
                datastore_ensure_key_index(int(r["id"]), r["cle"], bornee=False)
            except Exception as e:  # noqa: BLE001, SILENT — collecté, levé en bloc en fin de révision
                echecs.append((r["id"], e))
    _lever_si_echecs(echecs, "0029 upgrade")


def downgrade() -> None:
    echecs: list = []
    with op.get_context().autocommit_block():
        conn = op.get_bind().connection.driver_connection
        for r in _index_de_cle():
            if _MARQUE_REGLE not in r["def"]:
                continue
            ns = int(r["id"])
            tmp, nom = sql.Identifier(f"ds_bkey_{ns}_v2"), sql.Identifier(f"ds_bkey_{ns}")
            expr = sql.SQL(_V1).format(k=sql.Literal(r["cle"]))
            try:
                conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {t}").format(t=tmp))
                conn.execute(sql.SQL(
                    "CREATE UNIQUE INDEX CONCURRENTLY {t} ON datastore_rows (({e})) "
                    "WHERE ns_id = {ns} AND {e} IS NOT NULL"
                ).format(t=tmp, e=expr, ns=sql.Literal(ns)))
                conn.execute(sql.SQL("DROP INDEX IF EXISTS {n}").format(n=nom))
                conn.execute(sql.SQL("ALTER INDEX {t} RENAME TO {n}").format(t=tmp, n=nom))
            except Exception as e:  # noqa: BLE001, SILENT — collecté, levé en bloc en fin de révision
                conn.execute(sql.SQL("DROP INDEX IF EXISTS {t}").format(t=tmp))
                echecs.append((ns, e))
    _lever_si_echecs(echecs, "0029 downgrade")

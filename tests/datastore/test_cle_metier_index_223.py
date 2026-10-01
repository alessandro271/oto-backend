"""La clé métier compare la valeur que SERT la lecture, sur ses quatre chemins (oto#223).

Avant, trois règles pour une clé : l'index et le lookup comparaient le texte V1
(`COALESCE(data->'k'->>'valeur', data->>'k')`), les groupes de doublons et la fusion le
texte brut `data->>'k'`, la lecture la règle d'oto#163 (`unwrap`, `field_value_sql`).
Deux lignes qui servent la même valeur échappaient au contrôle ; deux lignes qui n'en
servent aucune se heurtaient, ou fusionnaient.

Contre un vrai PostgreSQL, parce que le sujet est ce que la BASE compare :
- cellule par cellule, la valeur servie (`unwrap`) confrontée à ce que comparent
  l'index, le lookup, les groupes de doublons et la fusion ;
- les quatre reproductions de l'issue, jouées sur les vraies fonctions ;
- la définition enregistrée de l'index et le plan du vrai lookup, figés : l'index est
  d'EXPRESSION, et le lookup ne le sert qu'à expression identique. Toucher la règle ou
  `LAYER_KEYS` fait rougir ce banc — et exige une migration qui reconstruit les index
  (modèle : révision `0029_cle_metier_valeur_servie`).
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql

#: Relevée sur PostgreSQL 17 le 30/09/2026 — la règle de lecture, au caractère près.
_REGLE = (
    "\nCASE\n"
    "    WHEN ((jsonb_typeof((data -> 'siren'::text)) = 'object'::text) AND "
    "((data -> 'siren'::text) ? 'valeur'::text)) THEN ((data -> 'siren'::text) ->> "
    "'valeur'::text)\n"
    "    WHEN ((jsonb_typeof((data -> 'siren'::text)) = 'object'::text) AND "
    "((data -> 'siren'::text) <> '{}'::jsonb) AND (((data -> 'siren'::text) - "
    "ARRAY['origine'::text, 'comment'::text, 'link'::text]) = '{}'::jsonb)) THEN "
    "NULL::text\n"
    "    ELSE (data ->> 'siren'::text)\n"
    "END")
INDEXDEF = (
    "CREATE UNIQUE INDEX ds_bkey_1 ON public.datastore_rows USING btree (("
    + _REGLE + ")) WHERE ((ns_id = 1) AND (" + _REGLE + " IS NOT NULL))")
EXPR_V1 = "COALESCE(data->'siren'->>'valeur', data->>'siren')"

MODES = ("force_custom_plan", "force_generic_plan")


@pytest.fixture()
def pg(pg_module_dsn, monkeypatch):
    """`datastore_rows` minimale, le module pointé dessus, aucun index."""
    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS datastore_rows")
        c.execute("CREATE TABLE datastore_rows ("
                  " ns_id INT, row_id TEXT, data JSONB,"
                  " created_at TIMESTAMPTZ DEFAULT now(),"
                  " updated_at TIMESTAMPTZ DEFAULT now())")
        yield c
        c.execute("DROP TABLE IF EXISTS datastore_rows")


@pytest.fixture()
def peuplee(pg):
    """Deux tableaux assez peuplés pour que le planner choisisse sans qu'on le force,
    l'index de `siren` posé sur le tableau 1."""
    pg.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES "
               "(1, 'a', '{\"siren\": \"552081317\"}'), "
               "(1, 'b', '{\"siren\": {\"valeur\": \"111111111\", \"comment\": \"registre\"}}')")
    pg.execute("INSERT INTO datastore_rows (ns_id, row_id, data) "
               "SELECT ns, ns || '-' || g, jsonb_build_object('siren', lpad(g::text, 9, '0')) "
               "FROM generate_series(1, 2) ns, generate_series(1, 3000) g")
    pg.execute("ANALYZE datastore_rows")
    from oto_mcp.db.datastore import datastore_ensure_key_index
    datastore_ensure_key_index(1, "siren")
    return pg


def _ins(c, rid: str, cellule, ns: int = 1, **autres) -> None:
    c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s::jsonb)",
              (ns, rid, json.dumps({"k": cellule, **autres})))


# --- cellule par cellule : servie = comparée ---------------------------------

TEMOINS = [
    pytest.param("123", id="scalaire-a-plat"),
    pytest.param({"valeur": "123"}, id="enveloppe-valeur"),
    pytest.param({"valeur": "123", "comment": "c", "origine": "o"}, id="valeur-et-couches"),
    pytest.param({"origine": ""}, id="couches-seules"),
    pytest.param({"comment": "à vérifier", "link": "x"}, id="couches-seules-2"),
    pytest.param({"valeur": None, "origine": "x"}, id="valeur-null"),
    pytest.param({"raison": "sociale", "n": 1}, id="objet-json-metier"),
    pytest.param(5, id="nombre"),
    pytest.param(True, id="booleen"),
]


def _texte_servi(cellule):
    """La valeur servie (`unwrap`), rendue en texte comme `data->>` la rend."""
    from oto_mcp.datastore.couches import unwrap
    v = unwrap(cellule)
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v)


@pytest.mark.parametrize("cellule", TEMOINS)
def test_l_index_compare_la_valeur_servie(pg, cellule):
    """L'expression de l'index ET celle de la lecture rendent la même chose : la
    valeur servie. Témoin : l'ancien texte V1 s'en écarte sur les cases sans valeur."""
    from oto_mcp.db.paths import bkey_index_expr, field_value_sql
    _ins(pg, "r", cellule)
    lu = pg.execute(sql.SQL("SELECT {i} AS i, {l} AS l FROM datastore_rows").format(
        i=bkey_index_expr("k"), l=field_value_sql("k"))).fetchone()
    servi = _texte_servi(cellule)
    if isinstance(cellule, dict) and "raison" in cellule:
        # Un objet métier se compare sur le texte JSONB de la base, pas sur un dump Python.
        servi = pg.execute("SELECT data->>'k' FROM datastore_rows").fetchone()[0]
    assert lu == (servi, servi)


@pytest.mark.parametrize("cellule", TEMOINS)
def test_le_lookup_trouve_la_ligne_par_sa_valeur_servie(pg, cellule):
    """Le lookup cherche la valeur DÉBALLÉE (ce que lui passent les écrivains) : il
    retrouve la ligne qui la sert, sous toutes ses formes — et aucune ligne quand la
    case n'en sert pas."""
    from oto_mcp.datastore.couches import unwrap
    from oto_mcp.db.datastore import datastore_find_row_id_by_key
    _ins(pg, "r", cellule)
    v = unwrap(cellule)
    if v is None:
        return  # aucun écrivain ne cherche une clé absente
    assert datastore_find_row_id_by_key(1, "k", v) == "r"


@pytest.mark.parametrize("cellule", TEMOINS)
def test_doublons_et_fusion_voient_ce_que_l_index_voit(pg, cellule):
    """Deux lignes : la cellule témoin, et la même valeur servie écrite à plat. Les
    groupes de doublons les voient ensemble exactement quand l'index les refuserait
    ensemble ; une cellule sans valeur servie ne fait doublon avec rien."""
    from oto_mcp.datastore.couches import unwrap
    from oto_mcp.db.datastore import (datastore_ensure_key_index,
                                      datastore_key_dup_groups,
                                      datastore_merge_key_duplicates)
    v = unwrap(cellule)
    _ins(pg, "r1", cellule, a=1)
    _ins(pg, "r2", cellule if v is None else v, b=2)
    groupes = datastore_key_dup_groups(1, "k")
    if v is None:
        assert groupes == []
        datastore_ensure_key_index(1, "k")       # rien à refuser
        assert datastore_merge_key_duplicates(1, "k") == 0
        return
    assert [g["n"] for g in groupes] == [2]
    assert datastore_merge_key_duplicates(1, "k") == 1
    reste = pg.execute("SELECT row_id, data FROM datastore_rows").fetchall()
    assert [r[0] for r in reste] == ["r1"]
    assert reste[0][1]["a"] == 1 and reste[0][1]["b"] == 2
    datastore_ensure_key_index(1, "k")           # posable après résorption


# --- les quatre reproductions de l'issue --------------------------------------

def test_repro_1_deux_cases_sans_valeur_ne_se_heurtent_pas(pg):
    from oto_mcp.db.datastore import datastore_ensure_key_index
    datastore_ensure_key_index(1, "k")
    _ins(pg, "r1", {"origine": ""})
    _ins(pg, "r2", {"origine": ""})        # V1 : UniqueViolation
    assert pg.execute("SELECT count(*) FROM datastore_rows").fetchone()[0] == 2


def test_repro_2_une_cle_nue_et_la_meme_enveloppee_sont_un_doublon(pg):
    from oto_mcp.db.datastore import (datastore_ensure_key_index,
                                      datastore_key_dup_groups,
                                      datastore_merge_key_duplicates)
    _ins(pg, "r1", "123")
    _ins(pg, "r2", {"valeur": "123", "comment": "c"})
    assert datastore_key_dup_groups(1, "k") == [{"value": "123", "n": 2}]
    assert datastore_merge_key_duplicates(1, "k") == 1
    datastore_ensure_key_index(1, "k")      # V1 : échouait à chaque passage


def test_repro_3_pas_de_faux_doublon_sur_une_enveloppe_sans_valeur(pg):
    from oto_mcp.db.datastore import datastore_key_dup_groups
    _ins(pg, "r1", {"comment": "à vérifier"})
    _ins(pg, "r2", {"comment": "à vérifier"})
    assert datastore_key_dup_groups(1, "k") == []


def test_repro_4_la_fusion_ne_confond_pas_deux_cles_vides(pg):
    from oto_mcp.db.datastore import datastore_merge_key_duplicates
    _ins(pg, "r1", {"origine": ""}, a=1)
    _ins(pg, "r2", {"origine": ""}, b=2)
    assert datastore_merge_key_duplicates(1, "k") == 0
    assert pg.execute("SELECT count(*) FROM datastore_rows").fetchone()[0] == 2


def test_le_lookup_rend_le_texte_comme_la_base(pg):
    """`True` en Python, `true` en base : le texte de la valeur cherchée est rendu par
    la base, pas par `str()` — sinon une clé booléenne ne se retrouvait jamais."""
    from oto_mcp.db.datastore import datastore_find_row_id_by_key
    _ins(pg, "r", True)
    _ins(pg, "s", 5, ns=2)
    assert datastore_find_row_id_by_key(1, "k", True) == "r"
    assert datastore_find_row_id_by_key(2, "k", "5") == "s"
    assert datastore_find_row_id_by_key(2, "k", 5) == "s"


# --- la définition et le plan, figés ------------------------------------------

def _plan(conn, requete: str, params: tuple, mode: str) -> str:
    """Le plan de la requête PRÉPARÉE, dans le mode demandé — le chemin qu'emprunte une
    requête que psycopg a préparée côté serveur."""
    texte = requete
    for i in range(1, len(params) + 1):
        texte = texte.replace("%s", f"${i}", 1)
    conn.execute(f"SET plan_cache_mode = {mode}")
    conn.execute(f"PREPARE lookup_223 AS {texte}")
    try:
        appel = sql.SQL("EXPLAIN EXECUTE lookup_223({})").format(
            sql.SQL(", ").join(sql.Literal(p) for p in params))
        return "\n".join(r[0] for r in conn.execute(appel))
    finally:
        conn.execute("DEALLOCATE lookup_223")
        conn.execute("RESET plan_cache_mode")


def _lookup_capture(monkeypatch, cle) -> tuple[str, tuple, object]:
    """Exécute le VRAI lookup et rend `(requête, paramètres, row_id trouvé)`, capturés sur
    sa connexion — rien n'est recomposé ici."""
    from oto_mcp.db import datastore as dsdb
    vu: dict = {}
    reel = dsdb._connect

    @contextmanager
    def espion():
        with reel() as conn:
            class _Connexion:
                def execute(self, requete, params=None):
                    vu["requete"] = requete.as_string(conn)
                    vu["params"] = tuple(params or ())
                    return conn.execute(requete, params)
            yield _Connexion()

    monkeypatch.setattr(dsdb, "_connect", espion)
    trouve = dsdb.datastore_find_row_id_by_key(1, "siren", cle)
    monkeypatch.undo()
    return vu["requete"], vu["params"], trouve


def test_la_definition_enregistree_par_postgres_est_la_regle_de_lecture(peuplee):
    indexdef = peuplee.execute("SELECT pg_get_indexdef('ds_bkey_1'::regclass)").fetchone()[0]
    assert indexdef == INDEXDEF


def test_le_lookup_trouve_une_cle_nue_comme_une_cle_enveloppee(peuplee, monkeypatch):
    assert _lookup_capture(monkeypatch, "552081317")[2] == "a"
    assert _lookup_capture(monkeypatch, "111111111")[2] == "b"


def test_en_plan_personnalise_le_lookup_est_servi_par_l_index(peuplee, monkeypatch):
    requete, params, _ = _lookup_capture(monkeypatch, "552081317")
    plan = _plan(peuplee, requete, params, "force_custom_plan")
    assert "Index Scan using ds_bkey_1" in plan, plan
    assert "Index Cond" in plan, plan


@pytest.mark.parametrize("mode", MODES)
def test_le_lookup_est_servi_par_l_index_dans_les_deux_plans(peuplee, monkeypatch, mode):
    """`ns_id` est un LITTÉRAL dans le lookup, comme dans le prédicat de l'index partiel
    (oto#225) : le prédicat se prouve aussi en plan GÉNÉRIQUE, celui que PostgreSQL peut
    retenir une fois que psycopg a préparé la requête. Le lookup rend la bonne ligne."""
    requete, params, trouve = _lookup_capture(monkeypatch, "552081317")
    assert trouve == "a"
    assert "ns_id = 1 " in requete and len(params) == 1, (requete, params)
    plan = _plan(peuplee, requete, params, mode)
    assert "Index Scan using ds_bkey_1" in plan, plan


def test_temoin_ns_id_en_parametre_perd_l_index_en_plan_generique(peuplee, monkeypatch):
    """Sans ce témoin, l'épreuve précédente pourrait être verte par construction : le même
    lookup, `ns_id` repassé en paramètre (la forme d'avant oto#225), ne trouve PAS l'index
    en plan générique — il lit tout le tableau."""
    requete, params, _ = _lookup_capture(monkeypatch, "552081317")
    parametree = requete.replace("ns_id = 1 ", "ns_id = %s ", 1)
    assert parametree != requete
    plan = _plan(peuplee, parametree, (1, *params), "force_generic_plan")
    assert "ds_bkey_1" not in plan, plan


def test_temoin_l_expression_V1_ne_sert_plus_l_index(peuplee, monkeypatch):
    """Sans ce témoin, l'épreuve du plan pourrait être verte par construction : la même
    requête écrite avec l'ancien texte V1 ne trouve PAS l'index."""
    from oto_mcp.db.paths import bkey_index_expr
    requete, params, _ = _lookup_capture(monkeypatch, "552081317")
    v1 = requete.replace(bkey_index_expr("siren").as_string(peuplee), EXPR_V1)
    assert v1 != requete
    plan = _plan(peuplee, v1, params, "force_custom_plan")
    assert "ds_bkey_1" not in plan, plan

"""Une valeur hors options se LIT comme toute valeur — contre un vrai PostgreSQL (oto#218).

L'avertissement de pose d'un enum disait ces lignes « INVISIBLES au filtrage et aux
facettes ». Le code ne fait rien de tel : aucune lecture ne consulte les `options`.
Ce banc est le TÉMOIN du texte servi : si un jour le filtre ou le regroupement se
mettent à écarter les valeurs hors options, il tombe, et le texte de
`_offending_enum_warning` doit changer avec lui.

Exemple synthétique de l'issue : une colonne `etat` aux options `oui`/`non`, et une
ligne existante `{"etat": "peut-être"}`.
"""
from __future__ import annotations

import json

import psycopg
import pytest

_OPTIONS = ["oui", "non"]


def _ddl() -> str:
    from oto_mcp.db import _schema
    s = _schema._SCHEMA
    i = s.index("CREATE TABLE IF NOT EXISTS datastore_rows")
    return s[i:s.index("\n);", i) + 3].replace(
        "REFERENCES user_datastores(id) ON DELETE CASCADE", "")


@pytest.fixture()
def pg(pg_module_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS datastore_rows")
        c.execute(_ddl())
        for rid, data in [("r-oui", {"etat": "oui"}),
                          ("r-non", {"etat": "non"}),
                          ("r-hors", {"etat": "peut-être"})]:
            c.execute("INSERT INTO datastore_rows (ns_id,row_id,data) "
                      "VALUES (1,%s,%s::jsonb)", (rid, json.dumps(data)))
        yield c
        c.execute("DROP TABLE IF EXISTS datastore_rows")


def test_the_scan_names_the_offending_value(pg):
    """Le point de départ : c'est bien la ligne que l'avertissement relève."""
    from oto_mcp import db
    bad = db.datastore_offending_enum_values(1, {"etat": _OPTIONS})
    assert bad == [{"field": "etat", "rows": 1, "distinct": 1,
                    "values": [{"value": "peut-être", "rows": 1}]}]


def test_eq_finds_the_offending_row(pg):
    from oto_mcp import db
    filtre = [{"field": "etat", "op": "eq", "value": "peut-être"}]
    assert [r["row_id"] for r in db.datastore_list_rows(1, filters=filtre)] == ["r-hors"]
    assert db.datastore_count_rows(1, filters=filtre) == 1


def test_grouping_counts_the_offending_value_as_its_own_group(pg):
    from oto_mcp import db
    groupes = {g["etat"]: g["count"] for g in db.datastore_aggregate(
        1, group_by="etat", metrics=[{"op": "count"}])}
    assert groupes == {"oui": 1, "non": 1, "peut-être": 1}


def test_the_typed_sort_puts_it_after_the_conformant_values(pg):
    """Le seul endroit où les options comptent à la lecture : le tri typé."""
    from oto_mcp import db
    for sens in ("asc", "desc"):
        ordre = [r["row_id"] for r in db.datastore_list_rows(
            1, order_by="etat", order_dir=sens, order_type="enum",
            order_options=_OPTIONS, limit=10)]
        assert ordre[-1] == "r-hors", sens

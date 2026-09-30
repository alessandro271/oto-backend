"""La révision 0029 reconstruit les index de clé métier sur la valeur servie (oto#223).

Jouée sur une vraie base bootée, ramenée à l'état d'avant ce lot : un tableau à clé
dont l'index porte le texte V1, un autre dont l'index est déjà neuf, un tableau sans
index. L'essai à blanc a déjà menti une fois (`docs/migrations-versionnees.md` §5) :
seule la définition relue dans le catalogue prouve que la révision a écrit.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

RACINE = Path(__file__).resolve().parents[2]
AVANT, APRES = "0028_partage_en_attente", "0029_cle_metier_valeur_servie"
V1 = "COALESCE(data->{k}->>'valeur', data->>{k})"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _def(dsn: str, ns: int):
    with psycopg.connect(dsn) as c:
        r = c.execute("SELECT pg_get_indexdef(to_regclass(%s))", (f"ds_bkey_{ns}",)).fetchone()
        return r[0] if r else None


def _poser_v1(dsn: str, ns: int, cle: str) -> None:
    """L'index tel que l'ancien code le posait."""
    e = sql.SQL(V1).format(k=sql.Literal(cle))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql.SQL("DROP INDEX IF EXISTS {n}").format(n=sql.Identifier(f"ds_bkey_{ns}")))
        c.execute(sql.SQL("CREATE UNIQUE INDEX {n} ON datastore_rows (({e})) "
                          "WHERE ns_id = {ns} AND {e} IS NOT NULL").format(
            n=sql.Identifier(f"ds_bkey_{ns}"), e=e, ns=sql.Literal(ns)))


@pytest.fixture()
def base_d_avant(live, pg_module_dsn):
    from alembic import command
    from oto_mcp import db
    from oto_mcp.db.datastore import datastore_ensure_key_index
    ids = {}
    tour = uuid.uuid4().hex[:8]
    for nom in ("v1", "neuf", "sans_index"):
        ns = db.create_datastore("user", "sub-essai-0029", f"cle-{nom}-{tour}")
        db.set_datastore_schema(ns, {"key": "siren", "fields": [{"key": "siren"}]})
        ids[nom] = ns
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for nom, ns in ids.items():
            c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES "
                      "(%s, 'a', '{\"siren\": \"552081317\"}'), "
                      "(%s, 'b', '{\"siren\": {\"valeur\": \"111111111\"}}'), "
                      "(%s, 'c', '{\"siren\": {\"origine\": \"\"}}')", (ns, ns, ns))
    _poser_v1(pg_module_dsn, ids["v1"], "siren")
    datastore_ensure_key_index(ids["neuf"], "siren")
    command.stamp(_alembic(), AVANT)
    yield ids
    command.stamp(_alembic(), "head")


def test_la_revision_reconstruit_sur_la_valeur_servie_et_se_defait(base_d_avant,
                                                                  pg_module_dsn):
    from alembic import command
    ids, cfg = base_d_avant, _alembic()
    assert "COALESCE" in _def(pg_module_dsn, ids["v1"])
    neuf_avant = _def(pg_module_dsn, ids["neuf"])
    assert "jsonb_typeof" in neuf_avant

    command.upgrade(cfg, APRES)
    for nom in ("v1", "neuf"):
        d = _def(pg_module_dsn, ids[nom])
        assert "jsonb_typeof" in d and "COALESCE" not in d, d
    assert _def(pg_module_dsn, ids["neuf"]) == neuf_avant
    assert _def(pg_module_dsn, ids["sans_index"]) is None, "un index manquant est l'affaire de la maintenance"
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        # La case sans valeur servie est HORS de l'index : une seconde passe.
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES "
                  "(%s, 'd', '{\"siren\": {\"origine\": \"\"}}')", (ids["v1"],))
        c.execute("DELETE FROM datastore_rows WHERE row_id = 'd'")

    command.downgrade(cfg, AVANT)
    for nom in ("v1", "neuf"):
        assert "COALESCE" in _def(pg_module_dsn, ids[nom])
    command.upgrade(cfg, APRES)
    assert "jsonb_typeof" in _def(pg_module_dsn, ids["v1"])


def test_un_doublon_sous_la_regle_fait_lever_la_revision_en_nommant_le_tableau(
        base_d_avant, pg_module_dsn):
    """Un index V1 absent de fait (retiré ici) couvre des doublons que la nouvelle règle
    voit : la révision lève en nommant le tableau, n'estampille rien, laisse les autres."""
    from alembic import command
    ids = base_d_avant
    ns = ids["v1"]
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute(sql.SQL("DROP INDEX {n}").format(n=sql.Identifier(f"ds_bkey_{ns}")))
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES "
                  "(%s, 'e', '{\"siren\": {\"valeur\": \"552081317\", \"comment\": \"x\"}}')",
                  (ns,))
        # Posé INVALIDE-équivalent : un index V1 qui ne voit pas le doublon enveloppé.
        c.execute(sql.SQL("CREATE UNIQUE INDEX {n} ON datastore_rows ((data->>'siren')) "
                          "WHERE ns_id = {ns} AND data->>'siren' IS NOT NULL").format(
            n=sql.Identifier(f"ds_bkey_{ns}"), ns=sql.Literal(ns)))
    with pytest.raises(RuntimeError, match=rf"tableau {ns}"):
        command.upgrade(_alembic(), APRES)
    with psycopg.connect(pg_module_dsn) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == AVANT
        assert c.execute("SELECT to_regclass(%s)", (f"ds_bkey_{ns}_v2",)).fetchone()[0] is None
    assert "jsonb_typeof" in _def(pg_module_dsn, ids["neuf"])
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DELETE FROM datastore_rows WHERE row_id = 'e' AND ns_id = %s", (ns,))

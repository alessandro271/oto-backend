"""`guides` retirée par une révision, plus par un geste manuel (oto-backend#1169).

La table est morte depuis le 23/09/2026 (oto#239, lot 1) ; son `DROP` était confié à
« l'opérationnel » par un commentaire de démarrage. La révision `0051_guides_retiree` le
rejoue partout où il manque, avec la purge des embeddings keyés sur `guides.id` :

```
base NEUVE              le démarrage ne crée pas la table ; la révision ne fait rien
base qui a la table     la révision la retire, et les embeddings `guide` ; le démarrage
                        ne la repose pas ; rejouée, elle ne fait rien
base déjà nettoyée      la révision ne fait rien, sans autre verrou qu'une lecture
```

Les gardes sans base de `test_search_reads_nodes.py` §4 tiennent le reste : plus aucun
code servi ne nomme la table.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
AVANT, REVISION = "0050_journal_totaux_jour", "0051_guides_retiree"

# La table telle que la production la portait avant oto#239 (index compris).
_DDL_D_AVANT = """
CREATE TABLE guides (
    id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, owner_id TEXT NOT NULL,
    slug TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'on-demand',
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (scope, owner_id, slug));
CREATE INDEX idx_guides_trgm ON guides USING GIN (body_md gin_trgm_ops);
"""
_VECTEUR = "('[' || array_to_string(array_fill(0.1::real, ARRAY[1024]), ',') || ']')::halfvec"


def _alembic(buffer=None):
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"), output_buffer=buffer)
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


# ── Sans base ─────────────────────────────────────────────────────────────────

def test_l_essai_a_blanc_imprime_les_deux_ordres(monkeypatch):
    from alembic import command
    monkeypatch.setenv("DATABASE_URL", "postgresql://essai:1/a-blanc")
    sortie = io.StringIO()
    command.upgrade(_alembic(sortie), f"{AVANT}:{REVISION}", sql=True)
    sql = sortie.getvalue()
    assert "DROP TABLE IF EXISTS guides" in sql
    assert "DELETE FROM aux_embeddings WHERE kind = 'guide'" in sql


# ── Sur une vraie base ────────────────────────────────────────────────────────

def _scalaire(dsn: str, sql: str):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql).fetchone()[0]


def _a_la_table(dsn: str) -> bool:
    return _scalaire(dsn, "SELECT to_regclass('guides') IS NOT NULL")


def _embeddings(dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        return dict(c.execute("SELECT kind, count(*) FROM aux_embeddings "
                              "WHERE ref = 424242 GROUP BY kind").fetchall())


def test_une_base_neuve_n_a_pas_la_table_et_la_revision_ne_fait_rien(live, pg_module_dsn):
    from alembic import command
    assert not _a_la_table(pg_module_dsn), "le démarrage ne doit pas créer la table"
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    assert not _a_la_table(pg_module_dsn)
    assert _scalaire(pg_module_dsn, "SELECT version_num FROM alembic_version") == REVISION


def test_une_base_qui_a_la_table_la_perd_avec_ses_embeddings(live, pg_module_dsn):
    import psycopg
    from alembic import command

    from oto_mcp.db import init_db
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute(_DDL_D_AVANT)
        c.execute("INSERT INTO guides (scope, owner_id, slug) VALUES ('org', '1', 'x')")
        for kind in ("guide", "node"):
            c.execute("INSERT INTO aux_embeddings (kind, ref, content_sha, embedding, model) "
                      f"VALUES (%s, 424242, 's', {_VECTEUR}, 'm')", (kind,))
    assert _a_la_table(pg_module_dsn), "le point de départ est celui d'une base d'avant"
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    assert not _a_la_table(pg_module_dsn), "la révision n'a pas retiré la table"
    assert _embeddings(pg_module_dsn) == {"node": 1}, \
        "les embeddings `guide` partent, ceux des nœuds restent"
    init_db()
    assert not _a_la_table(pg_module_dsn), "le démarrage a reposé la table"
    # Rejouée sur la base nettoyée : rien à faire, rien ne lève.
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    assert not _a_la_table(pg_module_dsn)
    assert _embeddings(pg_module_dsn) == {"node": 1}


def test_deja_faite_elle_ne_prend_aucun_verrou_au_dela_d_une_lecture(live, pg_module_dsn,
                                                                   monkeypatch):
    """Un verrou EXCLUSIVE tenu ailleurs sur `aux_embeddings` n'admet que des lectures :
    la révision, qui n'a rien à faire, passe sans l'attendre. Un ordre qui partait quand
    même attendrait, et l'attente est coupée à 2 s."""
    import psycopg
    from alembic import command

    assert not _a_la_table(pg_module_dsn)
    monkeypatch.setenv("DATABASE_URL",
                       pg_module_dsn + ("&" if "?" in pg_module_dsn else "?")
                       + "options=-c%20lock_timeout%3D2000")
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    with psycopg.connect(pg_module_dsn) as tenant:
        tenant.execute("LOCK TABLE aux_embeddings IN EXCLUSIVE MODE")
        command.upgrade(cfg, REVISION)
        tenant.rollback()
    assert _scalaire(pg_module_dsn, "SELECT version_num FROM alembic_version") == REVISION


def test_le_retour_arriere_leve_et_ne_touche_a_rien(live, pg_module_dsn):
    from alembic import command
    cfg = _alembic()
    command.stamp(cfg, REVISION)
    with pytest.raises(RuntimeError, match="irréversible"):
        command.downgrade(cfg, AVANT)
    assert _scalaire(pg_module_dsn, "SELECT version_num FROM alembic_version") == REVISION

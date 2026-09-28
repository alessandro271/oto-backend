"""La portée « personne, toutes orgs » (#1089) sur une base EXISTANTE : révision 0024,
jouée pour de vrai.

```
base d'avant    org_id NOT NULL, pas de contrainte de portée (forme d'après la 0023)
0024            + CHECK (org_id IS NOT NULL OR sub IS NOT NULL), org_id nullable —
                l'écriture en place (org_id posé) passe ENCORE
```

Le banc part d'une base bootée (forme cible), la RAMÈNE à la forme d'avant, au registre
de la 0023, et joue la révision — l'essai à blanc a déjà menti une fois
(`docs/migrations-versionnees.md` §5). Puis : le retour arrière refuse de lui-même tant
qu'une ligne sans org existe, et le démarrage après la révision est un no-op.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent

_INSERT = ("INSERT INTO org_entitlements (org_id, sub, right_key, source, value) "
           "VALUES (%s, %s, 'unipile_seats', %s, 3)")


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _forme(dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        nullable = c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'org_entitlements' AND column_name = 'org_id'").fetchone()[0]
        contraintes = {r[0] for r in c.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'org_entitlements'::regclass"
        ).fetchall()}
    return {"org_id_nullable": nullable == "YES",
            "une_portee": "org_entitlements_une_portee" in contraintes,
            "une_ligne": "org_entitlements_une_ligne" in contraintes}


CIBLE = {"org_id_nullable": True, "une_portee": True, "une_ligne": True}
AVANT = {"org_id_nullable": False, "une_portee": False, "une_ligne": True}


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command
    assert _forme(pg_module_dsn) == CIBLE, "une base NEUVE a la forme cible"
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE org_entitlements DROP CONSTRAINT org_entitlements_une_portee")
        c.execute("ALTER TABLE org_entitlements ALTER COLUMN org_id SET NOT NULL")
        org = c.execute("INSERT INTO orgs (name) VALUES ('org-avant') RETURNING id"
                        ).fetchone()[0]
        c.execute(_INSERT, (org, None, "subscription"))
    command.stamp(_alembic(), "0023_signature_webhook")
    assert _forme(pg_module_dsn) == AVANT
    return pg_module_dsn, org


def test_0024_ouvre_la_portee_sans_casser_l_ecriture_en_place(base_d_avant):
    import psycopg
    from alembic import command
    dsn, org = base_d_avant
    cfg = _alembic()
    with psycopg.connect(dsn, autocommit=True) as c, pytest.raises(psycopg.errors.NotNullViolation):
        c.execute(_INSERT, (None, "u-partout", "trial"))

    command.upgrade(cfg, "0024_droits_personne_partout")
    assert _forme(dsn) == CIBLE
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(_INSERT, (org, "u-dans-l-org", "subscription"))   # écriture en place
        c.execute(_INSERT, (None, "u-partout", "trial"))            # la nouvelle portée
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(_INSERT, (None, None, "offered"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(_INSERT, (None, "u-partout", "trial"))

    # Le retour arrière échoue DE LUI-MÊME tant qu'une ligne sans org existe (en-tête).
    with pytest.raises(Exception, match="NotNullViolation"):
        command.downgrade(cfg, "0023_signature_webhook")
    assert _forme(dsn) == CIBLE, "rien n'a été défait à moitié"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DELETE FROM org_entitlements WHERE org_id IS NULL")
    command.downgrade(cfg, "0023_signature_webhook")
    assert _forme(dsn) == AVANT
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM org_entitlements WHERE org_id = %s",
                         (org,)).fetchone()[0] == 2, "les lignes d'org restent"

    command.upgrade(cfg, "head")
    assert _forme(dsn) == CIBLE


def test_0024_est_idempotente_sur_la_forme_cible(base_d_avant):
    from alembic import command
    dsn, _ = base_d_avant
    cfg = _alembic()
    command.upgrade(cfg, "head")
    command.stamp(cfg, "0023_signature_webhook")
    command.upgrade(cfg, "0024_droits_personne_partout")
    assert _forme(dsn) == CIBLE


def test_le_demarrage_apres_la_revision_est_un_no_op(base_d_avant):
    from alembic import command
    from oto_mcp.db import init_db
    dsn, _ = base_d_avant
    command.upgrade(_alembic(), "head")
    init_db()
    assert _forme(dsn) == CIBLE

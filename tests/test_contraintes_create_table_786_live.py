"""La révision 0057 contre un VRAI PostgreSQL : les contraintes du CREATE TABLE remises à
une base servie qui ne les avait jamais reçues (#786).

Deux points de départ, les deux mesurés en production le 14/10/2026 :

- **déjà conforme** — une base née avec ces contraintes (le `CREATE TABLE` les pose) :
  la révision ne fait RIEN, et ne prend aucun verrou sur les tables ;
- **index uniques sans contraintes** — la base de l'instance principale : les index
  `uq_…` du démarrage, aucune des quatre contraintes. La révision la rend IDENTIQUE à une
  base neuve (contraintes, index, nullabilité), sans réécrire une ligne, et le
  démarrage suivant n'y change rien.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from oto_mcp.db import contraintes_create_table as cct

RACINE = Path(__file__).resolve().parents[1]
AVANT, REVISION = "0056_limite_refus_jour", cct.REVISION
TABLES = ("projects", "org_instructions", "org_instruction_revisions", "resource_grants")


@pytest.fixture()
def live(pg_dsn, monkeypatch):
    """Une base JETABLE par test, bootée par le vrai chemin — la forme des contraintes
    change au cours de ces tests. `DATABASE_URL` posée par `monkeypatch` (cf. la même
    fixture de `test_l7_shadow_origine_live.py` pour la raison)."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_c786_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    previous_pool = dbconn._pool
    monkeypatch.setenv("DATABASE_URL", dsn)
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _executer(dsn: str, *ordres: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        for ordre in ordres:
            c.execute(ordre)


def _etat(dsn: str) -> dict:
    """Le catalogue des quatre tables, les lignes, la version — ce que la révision ne
    doit changer QUE là où il manque une contrainte."""
    import psycopg
    noms = ", ".join(f"'{t}'" for t in TABLES)
    with psycopg.connect(dsn) as c:
        contraintes = c.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid), convalidated "
            f"FROM pg_constraint WHERE conrelid::regclass::text IN ({noms}) "
            "ORDER BY 1, 2").fetchall()
        index = c.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            f"WHERE tablename IN ({noms}) ORDER BY 1, 2").fetchall()
        nullables = c.execute(
            "SELECT attrelid::regclass::text, attname FROM pg_attribute "
            f"WHERE attrelid::regclass::text IN ({noms}) AND attnum > 0 "
            "AND NOT attisdropped AND NOT attnotnull ORDER BY 1, 2").fetchall()
        lignes = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
        version = c.execute("SELECT version_num FROM alembic_version").fetchone()
    return {"contraintes": contraintes, "index": index, "nullables": nullables,
            "lignes": lignes, "version": version[0] if version else None}


def _sans_version(etat: dict) -> dict:
    return {**etat, "version": None}


def _comme_l_instance_principale(dsn: str) -> None:
    """L'état mesuré le 14/10/2026 : les index uniques du démarrage, aucune des quatre
    contraintes. Avec des lignes, dont deux `mcp_slug` NULL (l'unicité les admet)."""
    _executer(dsn, *(f"ALTER TABLE {c.table} DROP CONSTRAINT {c.nom}" for c in cct.CLES),
              "ALTER TABLE resource_grants DROP CONSTRAINT resource_grants_role_check")
    _executer(
        dsn,
        "INSERT INTO projects (owner_type, owner_id, name, mcp_slug) VALUES "
        "('org', '1', 'a', 'publie'), ('org', '1', 'b', NULL), ('org', '1', 'c', NULL)",
        "INSERT INTO org_instructions (owner_type, owner_id, slug, body_md) VALUES "
        "('org', '1', 'p', 'x'), ('group', '1', 'p', 'y')",
        "INSERT INTO org_instruction_revisions (owner_type, owner_id, slug, version, body_md) "
        "VALUES ('org', '1', 'p', 1, 'x'), ('org', '1', 'p', 2, 'x')",
        "INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
        "principal_id, role) VALUES ('datastore_namespace', 'd', 'user', 'u', 'viewer'), "
        "('datastore_namespace', 'd', 'org', '1', 'manager')")


def test_deja_conforme_la_revision_ne_fait_rien(live):
    from alembic import command
    neuve = _etat(live)
    assert ("projects", "projects_mcp_slug_key", "UNIQUE (mcp_slug)", True) \
        in neuve["contraintes"], "une base neuve naît avec les contraintes du CREATE TABLE"
    command.stamp(_alembic(), AVANT)
    command.upgrade(_alembic(), REVISION)
    assert _sans_version(_etat(live)) == _sans_version(neuve)
    assert _etat(live)["version"] == REVISION


def test_deja_conforme_elle_ne_prend_aucun_verrou_sur_les_tables(live, monkeypatch):
    """Un verrou EXCLUSIVE tenu ailleurs sur les quatre tables n'admet que des lectures :
    la révision, qui n'a rien à faire, ne lit que le catalogue et passe sans l'attendre.
    Un ordre de DDL qui partait quand même attendrait, coupé à 2 s. (Pas ACCESS
    EXCLUSIVE : relire l'expression d'un CHECK prend l'`AccessShareLock` de sa table,
    celui de tout SELECT.)"""
    import psycopg
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", live + ("&" if "?" in live else "?")
                       + "options=-c%20lock_timeout%3D2000")
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    with psycopg.connect(live) as tenant:
        tenant.execute(f"LOCK TABLE {', '.join(TABLES)} IN EXCLUSIVE MODE")
        command.upgrade(cfg, REVISION)
        tenant.rollback()
    assert _etat(live)["version"] == REVISION


def _catalogue(etat: dict) -> dict:
    return {k: etat[k] for k in ("contraintes", "index", "nullables")}


def test_index_uniques_sans_contraintes_rejoint_une_base_neuve(live):
    from alembic import command

    from oto_mcp.db import init_db
    neuve = _etat(live)
    _comme_l_instance_principale(live)
    avant = _etat(live)
    assert not {c.nom for c in cct.CLES} & {x[1] for x in avant["contraintes"]}
    assert {"uq_projects_mcp_slug", "uq_org_instructions_owner_slug",
            "uq_org_instruction_revisions_owner"} <= {x[1] for x in avant["index"]}

    cfg = _alembic()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    apres = _etat(live)
    assert _catalogue(apres) == _catalogue(neuve), "identique à une base neuve"
    assert apres["lignes"] == avant["lignes"], "aucune ligne réécrite"
    assert apres["version"] == REVISION
    # Le démarrage suivant ne reconstruit rien (les `uq_…` sont restés), et la révision
    # rejouée ne fait rien.
    init_db()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    assert _etat(live) == apres


def test_les_contraintes_posees_mordent(live):
    """Posées par la révision, elles refusent ce que le CREATE TABLE refuse."""
    import psycopg
    from alembic import command

    _comme_l_instance_principale(live)
    command.stamp(_alembic(), AVANT)
    command.upgrade(_alembic(), REVISION)
    with pytest.raises(psycopg.errors.CheckViolation):
        _executer(live, "INSERT INTO resource_grants (resource_type, resource_id, "
                        "principal_type, principal_id, role) VALUES "
                        "('datastore_namespace', 'e', 'user', 'u', 'admin')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _executer(live, "INSERT INTO projects (owner_type, owner_id, name, mcp_slug) "
                        "VALUES ('org', '2', 'd', 'publie')")


def test_un_check_laisse_NOT_VALID_est_valide_a_la_reprise(live):
    from alembic import command
    _comme_l_instance_principale(live)
    _executer(live, cct.RESOURCE_GRANTS_ROLE.ddl_pose)
    command.stamp(_alembic(), AVANT)
    command.upgrade(_alembic(), REVISION)
    assert ("resource_grants", "resource_grants_role_check",
            cct.RESOURCE_GRANTS_ROLE.definition, True) in _etat(live)["contraintes"]


def test_une_ligne_hors_domaine_fait_echouer_la_validation_sans_rien_laisser_passer(live):
    """La vérification préalable du #786 est dans l'ordre lui-même : `VALIDATE` lève sur
    la première ligne fautive. La contrainte reste `NOT VALID` — elle refuse déjà toute
    écriture neuve — et la révision n'est pas estampillée."""
    import sqlalchemy.exc
    from alembic import command

    _comme_l_instance_principale(live)
    _executer(live, "INSERT INTO resource_grants (resource_type, resource_id, "
                    "principal_type, principal_id, role) VALUES "
                    "('datastore_namespace', 'f', 'user', 'u', 'intrus')")
    command.stamp(_alembic(), AVANT)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="resource_grants_role_check"):
        command.upgrade(_alembic(), REVISION)
    etat = _etat(live)
    assert etat["version"] == AVANT
    assert ("resource_grants", "resource_grants_role_check",
            cct.RESOURCE_GRANTS_ROLE.definition + " NOT VALID", False) in etat["contraintes"]


def test_un_nom_pris_par_une_autre_forme_leve_sans_rien_toucher(live):
    from alembic import command
    _comme_l_instance_principale(live)
    _executer(live, "ALTER TABLE projects ADD CONSTRAINT projects_mcp_slug_key "
                    "UNIQUE (mcp_slug, owner_id)")
    avant = _etat(live)
    command.stamp(_alembic(), AVANT)
    with pytest.raises(cct.ContrainteDivergente, match="projects_mcp_slug_key"):
        command.upgrade(_alembic(), REVISION)
    assert _sans_version(_etat(live)) == _sans_version(avant)


def test_le_retour_arriere_leve_et_ne_touche_a_rien(live):
    from alembic import command
    command.stamp(_alembic(), REVISION)
    avant = _etat(live)
    with pytest.raises(RuntimeError, match="stamp 0056_limite_refus_jour"):
        command.downgrade(_alembic(), AVANT)
    assert _etat(live) == avant


def test_l_essai_a_blanc_imprime_les_ordres(live, capsys):
    from alembic import command
    command.upgrade(_alembic(), f"{AVANT}:{REVISION}", sql=True)
    sortie = capsys.readouterr().out
    for cle in cct.CLES:
        assert cle.index.ddl_concurrent in sortie and cle.ddl_promotion in sortie
    assert cct.RESOURCE_GRANTS_ROLE.ddl_pose in sortie
    assert cct.RESOURCE_GRANTS_ROLE.ddl_validation in sortie

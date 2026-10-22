"""Le journal garde 30 jours en ligne, et cette borne n'a qu'une déclaration (22/10/2026).

Décision du 22/10/2026 : la rétention EN LIGNE de `tool_calls` passe de 90 à 30 jours ;
l'archive S3 mensuelle (export, relecture, puis suppression) ne change pas. Ce que ces
bancs verrouillent :

- **le défaut est 30**, lu par le script d'archive ET par la maintenance depuis la même
  déclaration (`oto_mcp/maintenance.py`) — deux constantes divergeraient en silence, et
  la plus courte gagnerait (#426) ;
- **les mois choisis** : le mois entier doit être sorti de la fenêtre. À 30 jours, au
  22/10 juillet et août partent, pas septembre (le 30/09 n'a pas 30 jours) ; septembre
  part le 31/10. L'horloge de la base est figée par une fonction `now()` placée devant
  `pg_catalog` dans le `search_path` : c'est la vraie requête qui est jouée.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

from oto_mcp import env_inventory, maintenance
from oto_mcp.db import _schema


def _script():
    chemin = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls_ret30", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS journal_archives, tool_calls")
        c.execute(re.search(r"^CREATE TABLE IF NOT EXISTS tool_calls \(.*?^\);",
                            _schema._SCHEMA, re.S | re.M).group(0))
        c.execute("SET TIME ZONE 'UTC'")
        for instant in ("2026-06-15 12:00", "2026-07-15 12:00", "2026-08-31 23:59",
                        "2026-09-01 00:00", "2026-09-30 23:59", "2026-10-01 00:00",
                        "2026-10-21 12:00"):
            c.execute("INSERT INTO tool_calls (created_at, sub, tool) VALUES (%s, 'u1', "
                      "'fr_search')", (instant + "+00",))
        c.execute("INSERT INTO tool_calls (created_at, sub, tool) VALUES "
                  "('2026-07-02 00:00+00', 'u1', 'run_start')")
        monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
        monkeypatch.setenv("OTO_MCP_S3_BUCKET", "seau")
        yield c
        c.execute("RESET TIME ZONE")


def _au(conn, instant: str) -> None:
    """Fige `now()` pour cette session : une fonction homonyme, devant `pg_catalog`."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS horloge_ret30")
    conn.execute("CREATE OR REPLACE FUNCTION horloge_ret30.now() RETURNS timestamptz "
                 f"LANGUAGE sql STABLE AS $$ SELECT '{instant}'::timestamptz $$")
    conn.execute("SET search_path TO horloge_ret30, pg_catalog, public")


def _mois(arch, conn, instant: str, jours: int) -> list[str]:
    _au(conn, instant)
    try:
        return [m for m, _ in arch._months_to_archive(conn, jours)]
    finally:
        conn.execute("RESET search_path")


def test_le_defaut_est_30_jours_et_n_a_qu_une_declaration(monkeypatch):
    monkeypatch.delenv("OTO_JOURNAL_RETENTION_DAYS", raising=False)
    assert maintenance.JOURNAL_RETENTION_DEFAULT_DAYS == 30
    assert maintenance.journal_retention_days() == 30
    # L'inventaire documente le défaut ; il ne peut pas en annoncer un autre.
    assert env_inventory.par_nom()["OTO_JOURNAL_RETENTION_DAYS"].defaut == "30"
    # Le script d'archive n'en porte aucune copie : il lit la déclaration du backend.
    source = (pathlib.Path(__file__).resolve().parents[2] / "deploy"
              / "archive_tool_calls.py").read_text(encoding="utf-8")
    assert "OTO_JOURNAL_RETENTION_DAYS\", \"" not in source
    assert _script().journal_retention_days is maintenance.journal_retention_days


@pytest.mark.parametrize("env, attendu", [(None, 30), ("45", 45)])
def test_le_script_d_archive_prend_la_retention_du_backend(base, monkeypatch, env, attendu):
    if env is None:
        monkeypatch.delenv("OTO_JOURNAL_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("OTO_JOURNAL_RETENTION_DAYS", env)
    arch = _script()
    vues: list[int] = []
    monkeypatch.setattr(arch, "_months_to_archive",
                        lambda conn, jours: vues.append(jours) or [])
    monkeypatch.setattr("sys.argv", ["archive_tool_calls.py", "--dry-run"])
    assert arch.main() == 0
    assert vues == [attendu]


def test_a_30_jours_le_22_10_juillet_et_aout_partent_pas_septembre(base, monkeypatch):
    monkeypatch.delenv("OTO_JOURNAL_RETENTION_DAYS", raising=False)
    arch = _script()
    jours = maintenance.journal_retention_days()
    assert _mois(arch, base, "2026-10-22 12:00+00", jours) == ["2026-06", "2026-07", "2026-08"]
    # La veille du 31/10, le 30/09 23:59 n'a pas encore 30 jours.
    assert _mois(arch, base, "2026-10-30 23:59+00", jours) == ["2026-06", "2026-07", "2026-08"]
    # Le 31/10, septembre est entièrement sorti de la fenêtre ; octobre jamais.
    assert _mois(arch, base, "2026-10-31 00:00+00", jours) == [
        "2026-06", "2026-07", "2026-08", "2026-09"]


def test_a_90_jours_le_22_10_seul_juin_partait(base):
    """Le contraste : avec l'ancienne borne, septembre attendait la fin décembre."""
    arch = _script()
    assert _mois(arch, base, "2026-10-22 12:00+00", 90) == ["2026-06"]
    assert _mois(arch, base, "2026-12-29 23:59+00", 90) == ["2026-06", "2026-07", "2026-08"]
    assert _mois(arch, base, "2026-12-30 00:00+00", 90) == [
        "2026-06", "2026-07", "2026-08", "2026-09"]

"""`idx_tool_calls_org_tool_ok` : construire soi-même seulement sur une petite table.

oto-backend#1145. Mesuré en production : 172 s de construction pour environ 12 M
lignes, au-delà des 120 s de la fenêtre de démarrage. Le verdict partagé par la
révision 0032 et le démarrage (`oto_mcp/db/index_concurrent.py`, l'index déclaré dans
`oto_mcp/db/index_releve.py`) refuse donc, en le
NOMMANT, de construire sur une grosse table, et de prendre un index invalide pour fait.
"""
from __future__ import annotations

import pytest

from oto_mcp.db import index_concurrent as ic
from oto_mcp.db.index_releve import RELEVE


def _faux(*, valide, trop_grosse=False, seuil=ic.CONSTRUCTION_MAX_LIGNES):
    vus: list[str] = []

    def scalaire(sql: str):
        vus.append(sql)
        if sql == RELEVE.sql_validite:
            return valide
        assert sql == RELEVE.sql_table_trop_grosse(seuil)
        return trop_grosse
    return scalaire, vus


def _table_de(lignes: int):
    """Une base dont la table compte `lignes` : le verdict lit `reltuples > seuil`."""
    vus: list[str] = []

    def scalaire(sql: str):
        vus.append(sql)
        if sql == RELEVE.sql_validite:
            return None
        seuil = int(sql.split("reltuples > ")[1].split(" ")[0])
        return lignes > seuil
    return scalaire, vus


def test_absent_sur_une_petite_table_se_construit():
    scalaire, _ = _faux(valide=None, trop_grosse=False)
    assert ic.a_construire(RELEVE, scalaire) is True


def test_absent_sur_une_grosse_table_renvoie_au_geste_manuel():
    scalaire, _ = _faux(valide=None, trop_grosse=True)
    with pytest.raises(ic.ConstructionManuelleRequise) as e:
        ic.a_construire(RELEVE, scalaire)
    assert "§5.1" in str(e.value) and "CONCURRENTLY" in str(e.value)


def test_invalide_n_est_ni_pris_pour_fait_ni_reconstruit():
    scalaire, vus = _faux(valide=False)
    with pytest.raises(ic.IndexInvalide) as e:
        ic.a_construire(RELEVE, scalaire)
    assert "DROP INDEX CONCURRENTLY" in str(e.value)
    # La taille n'est même pas lue : il n'y a rien à construire ici.
    assert vus == [RELEVE.sql_validite]


def test_valide_ne_fait_rien():
    scalaire, vus = _faux(valide=True)
    assert ic.a_construire(RELEVE, scalaire) is False
    assert vus == [RELEVE.sql_validite]


# --- le plafond imposé par l'appelant de `migrer` (#1195, porte d'une cible) -------------

def test_sans_plafond_impose_le_seuil_est_celui_de_l_index(monkeypatch):
    monkeypatch.delenv(ic.PLAFOND_ENV, raising=False)
    assert ic.plafond_impose() is None
    scalaire, _ = _table_de(150_000)
    with pytest.raises(ic.ConstructionManuelleRequise):
        ic.a_construire(RELEVE, scalaire, max_lignes=ic.plafond_impose())


def test_le_plafond_impose_construit_en_dessous_et_refuse_au_dessus(monkeypatch):
    monkeypatch.setenv(ic.PLAFOND_ENV, "10000000")
    assert ic.plafond_impose() == 10_000_000
    scalaire, _ = _table_de(5_000_000)
    assert ic.a_construire(RELEVE, scalaire, max_lignes=ic.plafond_impose()) is True
    scalaire, _ = _table_de(15_000_000)
    with pytest.raises(ic.ConstructionManuelleRequise) as e:
        ic.a_construire(RELEVE, scalaire, max_lignes=ic.plafond_impose())
    assert "10000000 lignes" in str(e.value)


@pytest.mark.parametrize("brut", ["dix", "0", "-5", "1.5"])
def test_un_plafond_illisible_est_une_erreur_nommee(monkeypatch, brut):
    monkeypatch.setenv(ic.PLAFOND_ENV, brut)
    with pytest.raises(ic.PlafondInvalide, match=ic.PLAFOND_ENV):
        ic.plafond_impose()


def test_la_revision_lit_le_plafond_la_porte_ne_le_voit_pas(monkeypatch):
    """`poser_par_revision` passe le plafond au verdict ; sans plafond, le seuil de l'index."""
    monkeypatch.setenv(ic.PLAFOND_ENV, "10000000")
    scalaire, vus = _table_de(5_000_000)
    executes: list[str] = []

    class _Bind:
        def exec_driver_sql(self, sql):
            class _R:
                def scalar(_self, _sql=sql):
                    return True if "indisvalid" in _sql and executes else scalaire(_sql)
            return _R()

    class _Ctx:
        def autocommit_block(self):
            import contextlib
            return contextlib.nullcontext()

    class _Op:
        def get_context(self): return _Ctx()
        def get_bind(self): return _Bind()
        def execute(self, sql): executes.append(sql)

    ic.poser_par_revision(_Op(), RELEVE)
    assert RELEVE.ddl_concurrent in executes
    assert any("reltuples > 10000000" in v for v in vus)


def test_le_demarrage_ignore_le_plafond_impose(monkeypatch, caplog):
    """La fenêtre de démarrage bloque les écritures : son seuil ne se relève pas."""
    monkeypatch.setenv(ic.PLAFOND_ENV, "10000000")
    scalaire, vus = _table_de(5_000_000)
    executes: list[str] = []

    class _Conn:
        def execute(self, sql):
            executes.append(sql)
            class _Cur:
                def fetchone(_self):
                    v = scalaire(sql)
                    return {"v": v}
            return _Cur()

    import logging
    with caplog.at_level(logging.ERROR):
        ic.poser_au_demarrage(_Conn(), RELEVE)
    assert RELEVE.ddl_demarrage not in executes
    assert any(f"reltuples > {ic.CONSTRUCTION_MAX_LIGNES}" in v for v in vus)
    assert "dépasse 100000 lignes" in caplog.text


def test_le_verdict_contre_une_vraie_base(live):
    """Le SQL réel : l'index que le démarrage a posé sur la base neuve est valide ; une
    fois retiré, il est « à construire » sous le seuil et refusé au-dessus."""
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        scalaire = ic.scalaire_de(conn)
        assert scalaire(RELEVE.sql_validite) is True
        assert ic.a_construire(RELEVE, scalaire) is False

        conn.execute(f"DROP INDEX {RELEVE.nom}")
        conn.execute("INSERT INTO tool_calls (tool) SELECT 'essai' FROM generate_series(1, 3)")
        assert ic.a_construire(RELEVE, scalaire) is True
        with pytest.raises(ic.ConstructionManuelleRequise):
            ic.a_construire(RELEVE, scalaire, max_lignes=1)
        conn.rollback()


# ── Les ouvertures de runs (infra#9, révision 0049) ──────────────────────────

def test_la_page_des_runs_porte_le_predicat_des_index_d_ouvertures():
    """Un index partiel ne sert que la requête qui IMPLIQUE son prédicat : celui des
    trois index d'ouvertures doit figurer, mot pour mot, dans le choix de page."""
    from oto_mcp.db import usage
    from oto_mcp.db.index_releve import OUVERTURES, PREDICAT_OUVERTURE

    sql = usage._derniers_runs(" AND d.org_id = %s")
    assert "d.tool = 'run_start' AND d.run_id IS NOT NULL" in sql
    assert "ORDER BY d.created_at DESC, d.id DESC" in sql
    for index in OUVERTURES:
        assert index.forme.endswith(f"WHERE {PREDICAT_OUVERTURE}")
        assert "created_at DESC, id DESC)" in index.forme


def test_la_revision_0049_pose_les_index_d_ouvertures():
    import importlib.util
    from pathlib import Path

    from oto_mcp.db.index_releve import OUVERTURES, REVISION_OUVERTURES

    (chemin,) = Path("oto_mcp/db/migrations/versions").glob("*_0049_*.py")
    spec = importlib.util.spec_from_file_location("rev_0049", chemin)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert rev.revision == REVISION_OUVERTURES and len(rev.revision) <= 32
    assert all(i.revision == REVISION_OUVERTURES for i in OUVERTURES)
    # Le geste manuel nomme la commande versionnée qui pose les index de 0049.
    assert f"index-concurrents {REVISION_OUVERTURES}" in OUVERTURES[0].procedure


def test_les_index_d_ouvertures_servent_les_trois_pages(live):
    """Contre une vraie base : le démarrage les a posés VALIDES, et chacune des trois
    pages (plateforme, org, compte × org) passe par le sien — dans son ordre, sans tri."""
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.index_releve import OUVERTURES

    pages = {
        "idx_tool_calls_run_start": "",
        "idx_tool_calls_run_start_org": " AND d.org_id = 7",
        "idx_tool_calls_run_start_sub": " AND d.sub = 'u' AND d.org_id = 7",
    }
    with _connect() as conn:
        scalaire = ic.scalaire_de(conn)
        for index in OUVERTURES:
            assert scalaire(index.sql_validite) is True, index.nom
        conn.execute(
            "INSERT INTO tool_calls (tool, kind, sub, org_id, run_id, created_at) "
            "SELECT CASE WHEN g % 50 = 0 THEN 'run_start' ELSE 'outil' END, 'mcp', "
            "'u' || (g % 7), g % 11, 'r-' || g, now() - g * interval '1 minute' "
            "FROM generate_series(1, 20000) g")
        conn.execute("ANALYZE tool_calls")
        for nom, portee in pages.items():
            plan = "\n".join(next(iter(r.values())) for r in conn.execute(
                "EXPLAIN SELECT d.id FROM tool_calls d WHERE d.tool = 'run_start' "
                f"AND d.run_id IS NOT NULL{portee} "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT 5").fetchall())
            assert nom in plan and "Sort" not in plan, plan
        conn.rollback()

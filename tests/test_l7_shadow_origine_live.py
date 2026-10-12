"""L'ORIGINE d'une observation de la fenêtre L7, contre un VRAI PostgreSQL.

**Le problème que ça règle.** Prod et preprod partagent la même base. Le premier
compteur de la fenêtre L7 mélangeait donc les deux — 134 observations dont on ne
pouvait pas dire lesquelles venaient de la production, alors que c'est exactement la
question qui autorise la bascule d'autorité.

**Ce que seul un vrai serveur peut dire**, et qui est tout l'objet de ce fichier :

- la clé porte l'origine : un index unique `NULLS NOT DISTINCT`, sans clé primaire. Une
  base neuve la reçoit du démarrage ; une base servie, de la révision
  `0052_shadow_l7_cle_origine` (oto-backend#1169), qui produit l'état final de l'ancien
  script manuel `migrate_shadow_origine` — index d'abord, ancienne clé retirée ensuite ;
- `NULLS NOT DISTINCT` : deux lignes d'origine INCONNUE ne doivent pas se dupliquer.
  C'est un comportement du moteur, pas du code ;
- sous l'ANCIENNE clé, une seconde origine LÈVE en nommant la révision : plus de repli
  qui mélangerait les compteurs en silence ;
- la lentille compte ce qu'elle DIT compter, jusqu'à la validation du modèle servi.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
AVANT, REVISION = "0051_guides_retiree", "0052_shadow_l7_cle_origine"
# L'état final de l'ancien script manuel, tel que PostgreSQL le relit.
INDEXDEF = ("CREATE UNIQUE INDEX access_shadow_l7_origine_key ON public.access_shadow_l7 "
            "USING btree (day, connector, org_id, classe, origine) NULLS NOT DISTINCT")


@pytest.fixture()
def live(pg_dsn, monkeypatch):
    """Une base JETABLE par test — la forme de la CLÉ change au cours de ces tests,
    donc les partager les rendrait dépendants de l'ordre.

    ⚠️ `DATABASE_URL` est posée par `monkeypatch`, jamais à la main : un test qui la
    repose lui-même par `monkeypatch.setenv` (le `lock_timeout` de
    `test_deja_faite_…`) voit sa remise jouée APRÈS le démontage de cette fixture —
    `monkeypatch` est installé avant elle, par une fixture autouse du conftest. Posée à
    la main, la variable était rendue ici, puis `monkeypatch` remettait par-dessus
    le DSN de cette base, détruite entre-temps : le reste du worker ouvrait son pool
    dessus (`database "oto_l7o_…" does not exist`, PoolTimeout dans des bancs sans
    rapport, rouge du tronc du 12/10/2026). Par `monkeypatch`, les remises se
    défont dans l'ordre inverse des poses."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_l7o_" + uuid.uuid4().hex[:8]
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


def _lignes():
    from oto_mcp.db import access_shadow as db_shadow
    return db_shadow.read_shadow(days=1)


# ── L'origine est celle que le process DÉCLARE, jamais posée à la main ────────

@pytest.mark.parametrize("declare", ["prod", "preprod"])
def test_l_origine_est_celle_que_le_process_declare(live, monkeypatch, declare):
    from oto_mcp.db import access_shadow as db_shadow

    monkeypatch.setenv("OTO_ENV", declare)
    db_shadow.bump_shadow("serper", 7, "accord", 1)
    assert [l["origine"] for l in _lignes()] == [declare]


def test_sans_declaration_l_origine_reste_INCONNUE(live, monkeypatch):
    """Un environnement qui ne se nomme pas ne se devine pas. C'est ce qui sépare cette
    lecture de `project_domain()`, dont le défaut est le domaine de PRODUCTION : une
    variable oubliée y serait classée « prod » en silence."""
    from oto_mcp.db import access_shadow as db_shadow

    monkeypatch.delenv("OTO_ENV", raising=False)
    db_shadow.bump_shadow("serper", 7, "accord", 1)
    assert [l["origine"] for l in _lignes()] == [None]


# ── La clé porte l'origine ────────────────────────────────────────────────────

def test_les_deux_origines_ont_chacune_leur_ligne(live):
    from oto_mcp.db import access_shadow as db_shadow

    db_shadow.bump_shadow("serper", 7, "accord", 2, origine="prod")
    db_shadow.bump_shadow("serper", 7, "accord", 3, origine="preprod")
    db_shadow.bump_shadow("serper", 7, "accord", 1, origine="prod")
    par_origine = {l["origine"]: l["n"] for l in _lignes()}
    assert par_origine == {"prod": 3, "preprod": 3}


def test_les_lignes_d_origine_inconnue_se_cumulent_sur_une_seule(live):
    from oto_mcp.db import access_shadow as db_shadow

    db_shadow.bump_shadow("serper", 7, "accord", 1, origine=None)
    db_shadow.bump_shadow("serper", 7, "accord", 4, origine=None)
    assert [(l["origine"], l["n"]) for l in _lignes()] == [(None, 5)]


def test_la_cle_etendue_refuse_DEUX_lignes_d_origine_inconnue(live):
    """`NULLS NOT DISTINCT`, exercé là où il agit VRAIMENT — sur l'INSERT.

    L'écriture normale ne l'atteint jamais : son UPDATE compare avec `IS NOT DISTINCT
    FROM`, donc elle retrouve la ligne NULL et n'insère pas. L'index protège l'autre
    cas, celui d'une COURSE — deux process qui ne trouvent rien et insèrent tous les
    deux. Sans la clause, les deux réussissent et le compteur se dédouble en silence.
    (Écrit après qu'une mutation a montré que le test d'origine ne prouvait rien : il
    passait aussi bien avec la clause que sans.)"""
    import psycopg
    from oto_mcp.db._conn import _connect

    insert = ("INSERT INTO access_shadow_l7 (day, connector, org_id, classe, n, origine) "
              "VALUES (CURRENT_DATE, 'serper', 7, 'accord', 1, NULL)")
    with _connect() as conn:
        conn.execute(insert)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with _connect() as conn:
            conn.execute(insert)


def test_sous_l_ANCIENNE_cle_une_seconde_origine_leve_en_nommant_la_revision(live):
    """L'état d'une base servie avant la révision. Mélanger les deux environnements
    dans une ligne est précisément ce que la colonne existe pour empêcher : on le dit."""
    from oto_mcp.db import access_shadow as db_shadow

    _ancienne_forme(live)
    db_shadow.bump_shadow("serper", 7, "accord", 2, origine="prod")
    with pytest.raises(RuntimeError, match=REVISION):
        db_shadow.bump_shadow("serper", 7, "accord", 3, origine="preprod")
    assert [(l["origine"], l["n"]) for l in _lignes()] == [("prod", 2)]


# ── La révision 0052 ──────────────────────────────────────────────────────────

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


def _ancienne_forme(dsn: str, *, avec_index: bool = False) -> None:
    """Ramène la table à la forme d'avant la clé étendue (clé primaire sans l'origine),
    l'index en plus si `avec_index` (une base servie dont le démarrage l'a posé)."""
    _executer(dsn, "DROP INDEX access_shadow_l7_origine_key",
              "ALTER TABLE access_shadow_l7 ADD CONSTRAINT access_shadow_l7_pkey "
              "PRIMARY KEY (day, connector, org_id, classe)")
    if avec_index:
        _executer(dsn, "CREATE UNIQUE INDEX access_shadow_l7_origine_key ON "
                       "access_shadow_l7 (day, connector, org_id, classe, origine) "
                       "NULLS NOT DISTINCT")


def _etat(dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        pk = c.execute("SELECT 1 FROM pg_constraint WHERE conrelid = "
                       "'access_shadow_l7'::regclass AND contype = 'p'").fetchone()
        index = c.execute("SELECT pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
                          "WHERE i.indexrelid = to_regclass('access_shadow_l7_origine_key')"
                          ).fetchone()
        lignes = c.execute("SELECT origine, n FROM access_shadow_l7 "
                           "ORDER BY origine NULLS FIRST").fetchall()
        version = c.execute("SELECT version_num FROM alembic_version").fetchone()
    return {"pk": pk is not None, "index": index, "lignes": lignes,
            "version": version[0] if version else None}


def test_une_base_neuve_nait_dans_l_etat_final_et_la_revision_ne_fait_rien(live):
    from alembic import command
    neuve = _etat(live)
    assert neuve["pk"] is False and neuve["index"] == (INDEXDEF, True)
    command.stamp(_alembic(), AVANT)
    command.upgrade(_alembic(), REVISION)
    assert {**_etat(live), "version": None} == {**neuve, "version": None}
    assert _etat(live)["version"] == REVISION


@pytest.mark.parametrize("avec_index", [False, True],
                         ids=["ancienne-cle-seule", "index-pose-par-le-demarrage"])
def test_l_ancienne_forme_rejoint_l_etat_final_sans_toucher_aux_lignes(live, avec_index):
    from alembic import command

    from oto_mcp.db import init_db
    _ancienne_forme(live, avec_index=avec_index)
    _executer(live, "INSERT INTO access_shadow_l7 (day, connector, org_id, classe, n, origine) "
                    "VALUES (CURRENT_DATE, 'serper', 7, 'accord', 3, NULL), "
                    "(CURRENT_DATE, 'serper', 7, 'refus', 2, 'prod')")
    avant = _etat(live)
    assert avant["pk"] is True
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    apres = _etat(live)
    assert apres["pk"] is False, "l'ancienne clé primaire doit partir"
    assert apres["index"] == (INDEXDEF, True), "la clé étendue, valide, comme l'ancien script"
    assert apres["lignes"] == avant["lignes"], "aucune ligne réécrite"
    assert apres["version"] == REVISION
    # Le démarrage ne repose pas l'ancienne clé ; rejouée, la révision ne fait rien.
    init_db()
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)
    assert _etat(live) == apres


def test_deja_faite_elle_ne_prend_aucun_verrou_sur_la_table(live, monkeypatch):
    """Un verrou EXCLUSIVE tenu ailleurs sur la table n'admet que des lectures : la
    révision, qui n'a rien à faire, ne lit que le catalogue et passe sans l'attendre. Un
    ordre qui partait quand même attendrait, et l'attente est coupée à 2 s."""
    import psycopg
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", live + ("&" if "?" in live else "?")
                       + "options=-c%20lock_timeout%3D2000")
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    with psycopg.connect(live) as tenant:
        tenant.execute("LOCK TABLE access_shadow_l7 IN ACCESS EXCLUSIVE MODE")
        command.upgrade(cfg, REVISION)
        tenant.rollback()
    assert _etat(live)["version"] == REVISION


def test_un_index_invalide_leve_sans_retirer_la_cle(live):
    from alembic import command

    from oto_mcp.db.index_concurrent import IndexInvalide
    _ancienne_forme(live, avec_index=True)
    _executer(live, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                    "'access_shadow_l7_origine_key'::regclass")
    command.stamp(_alembic(), AVANT)
    with pytest.raises(IndexInvalide):
        command.upgrade(_alembic(), REVISION)
    assert _etat(live)["pk"] is True, "l'ancienne clé reste tant que l'index ne garde rien"


def test_le_retour_arriere_leve_et_ne_touche_a_rien(live):
    from alembic import command
    command.stamp(_alembic(), REVISION)
    avant = _etat(live)
    with pytest.raises(RuntimeError, match="irréversible"):
        command.downgrade(_alembic(), AVANT)
    assert _etat(live) == avant


# ── La lentille compte ce qu'elle dit compter ─────────────────────────────────

def test_la_lentille_ne_compte_que_l_origine_demandee(live):
    from oto_mcp.capabilities import access_shadow_admin as lentille
    from oto_mcp.db import access_shadow as db_shadow

    db_shadow.bump_shadow("serper", 7, "accord", 9, origine="prod")
    db_shadow.bump_shadow("serper", 7, "accord", 4, origine="preprod")
    db_shadow.bump_shadow("serper", 7, "accord", 2, origine=None)

    prod = lentille.ShadowOut(**lentille._read(None, lentille.AccessShadowInput()))
    assert prod.origine == "prod" and prod.verdict.origine == "prod"
    assert prod.verdict.observations == 9, "la preprod et l'inconnu ne comptent pas"
    assert prod.verdict.origine_inconnue == 2, "…mais l'inconnu est DIT"
    assert [l.origine for l in prod.lignes] == ["prod"]

    toutes = lentille.ShadowOut(**lentille._read(
        None, lentille.AccessShadowInput(origine="toutes")))
    assert toutes.verdict.observations == 15
    assert {l.origine for l in toutes.lignes} == {"prod", "preprod", None}


def test_une_fenetre_prod_VIDE_ne_s_ouvre_pas_meme_avec_de_l_inconnu(live):
    """Le cas du 2026-08-29 : 134 observations en base, aucune attribuable à la prod.
    La porte doit rester fermée, et la réponse doit dire POURQUOI — sinon on lit
    « rien ne s'est passé » là où il faut lire « on ne sait pas qui a écrit »."""
    from oto_mcp.capabilities import access_shadow_admin as lentille
    from oto_mcp.db import access_shadow as db_shadow

    db_shadow.bump_shadow("serper", 7, "accord", 134, origine=None)
    out = lentille.ShadowOut(**lentille._read(None, lentille.AccessShadowInput()))
    assert out.verdict.observations == 0
    assert out.verdict.porte_ouverte is False
    assert out.verdict.origine_inconnue == 134

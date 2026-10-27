"""`claim_next_job` doit passer par l'index partiel des travaux VIVANTS, jamais
par un parcours complet de `runner_jobs` (mesuré en production le 17/09/2026,
`Seq Scan`, 72 425 lignes `done` écartées, 152 ms, pour zéro résultat).

Cause : l'ancien index (`idx_runner_jobs_claim`, `WHERE status = 'pending'`) ne
couvrait que la moitié de la condition de réservation (`status = 'pending' OR
(status = 'claimed' AND lease_until < NOW())`) — le planificateur ne pouvait pas
s'y limiter face à une disjonction sur DEUX statuts.

Et la part équitable (27/10/2026) doit compter la charge en vol UNE fois par
réservation : une sous-requête corrélée dans l'`ORDER BY` la recomptait pour chaque
ligne éligible — quadratique en rafale, et invisible à un banc qui ne lit que le
TYPE des nœuds.

⚠️ Le plan se lit sur la requête que `claim_next_job` ÉMET (un espion sur sa
connexion la capte, avec ses paramètres), jamais sur une copie à la main : la copie
d'avant ce lot décrivait encore l'ancien tri quand le code en avait changé.

⚠️ Ce banc exige un vrai PostgreSQL et une VRAIE volumétrie (des dizaines de
milliers de lignes `done`, et une rafale de travaux dus) — un plan ne se lit pas
sur une table vide, où PostgreSQL choisirait un `Seq Scan` de toute façon (moins
cher qu'un index sur une poignée de pages). Aucun seuil de durée : le TYPE des
nœuds, et le nombre de fois qu'ils s'exécutent (`Actual Loops`).
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def base_avec_historique(pg_dsn):
    """Une base migrée (Alembic head), peuplée comme la production : ~70k lignes
    `done`, une poignée `pending`/`claimed` — jamais l'inverse (le défaut du
    17/09 ne se voit QUE sous cette proportion)."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_runner_idx_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom

    url_prec, pool_prec = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, "
                "attempts, max_attempts) "
                "SELECT 1, 'start', 'done', NOW() - (g || ' seconds')::interval, "
                "1, 3 FROM generate_series(1, 70000) g")
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, "
                "attempts, max_attempts) "
                "SELECT 1, 'start', 'pending', "
                "NOW() - (g || ' seconds')::interval, 0, 3 "
                "FROM generate_series(1, 5) g")
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, "
                "attempts, max_attempts, lease_until) "
                "SELECT 1, 'start', 'claimed', "
                "NOW() - (g || ' seconds')::interval, 1, 3, "
                "NOW() - interval '1 minute' FROM generate_series(1, 3) g")
            # La RAFALE : deux orgs, 300 travaux dus chacune, et quelques travaux
            # en vol à bail vivant — ce que la part équitable doit trier.
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, "
                "attempts, max_attempts) "
                "SELECT o, 'start', 'pending', "
                "NOW() - (g || ' seconds')::interval, 0, 3 "
                "FROM generate_series(1, 300) g, (VALUES (2), (3)) v(o)")
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, "
                "attempts, max_attempts, lease_until) "
                "SELECT o, 'start', 'claimed', NOW() - interval '1 hour', 1, 3, "
                "NOW() + interval '10 minutes' "
                "FROM generate_series(1, 3) g, (VALUES (2), (3)) v(o)")
            c.execute("ANALYZE runner_jobs")
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_prec
        if url_prec is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_prec
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _plan(dsn, sql, params):
    import psycopg
    with psycopg.connect(dsn) as c:
        lignes = c.execute(f"EXPLAIN {sql}", params).fetchall()
    return "\n".join(l[0] for l in lignes)


class _PriseNonJouee:
    """Ce que l'espion rend à la place de la réservation : « file vide ». La requête
    est captée, jamais exécutée — la base du banc ne bouge pas d'un sondage à
    l'autre."""

    def fetchone(self):
        return None


def _requete_emise(monkeypatch, org_id):
    """La requête de réservation (`WITH … pris AS …`) que `claim_next_job` émet
    RÉELLEMENT pour `org_id`, et ses paramètres — captés sur sa connexion. Les autres
    requêtes du sondage (présence, épaves, périmés) s'exécutent normalement."""
    import contextlib

    from oto_mcp import db
    from oto_mcp.db import runner_jobs as module
    captees: list = []
    vrai = module._connect

    class _Espion:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "pris AS (" in sql:
                captees.append((sql, params))
                return _PriseNonJouee()
            return self._conn.execute(sql, params)

        def __getattr__(self, nom):
            return getattr(self._conn, nom)

    @contextlib.contextmanager
    def _espionne():
        with vrai() as conn:
            yield _Espion(conn)

    monkeypatch.setattr(module, "_connect", _espionne)
    assert db.claim_next_job(org_id, "w-banc-plan") is None
    monkeypatch.undo()
    assert len(captees) == 1, captees
    return captees[0]


def _noeuds(plan: dict):
    """Tous les nœuds du plan, sous-plans (`SubPlan`, `InitPlan`) compris."""
    yield plan
    for enfant in plan.get("Plans", []) or []:
        yield from _noeuds(enfant)


def _executions_de_runner_jobs(dsn, sql, params) -> list:
    """Pour chaque nœud qui lit `runner_jobs` : (type, `Actual Loops`) — mesuré par
    `EXPLAIN (ANALYZE, BUFFERS)`, dans une transaction ANNULÉE (la réservation est un
    `UPDATE`).

    ⚠️ `Actual Loops`, pas la présence du nœud : un sous-plan apparaît dans l'arbre
    même quand il n'est jamais exécuté."""
    import psycopg
    with psycopg.connect(dsn) as c:
        plan = c.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                         params).fetchone()[0][0]["Plan"]
        c.rollback()
    return [(n["Node Type"], n.get("Actual Loops", 0)) for n in _noeuds(plan)
            if n.get("Relation Name") == "runner_jobs"]


def test_le_plan_passe_par_l_index_partiel_pas_un_parcours_complet(
        base_avec_historique, monkeypatch):
    sql, params = _requete_emise(monkeypatch, None)
    plan = _plan(base_avec_historique, sql, params)
    assert "idx_runner_jobs_live" in plan, (
        "la réservation n'utilise pas idx_runner_jobs_live :\n" + plan)
    assert "Seq Scan on runner_jobs" not in plan, (
        "la réservation redevient un parcours complet de runner_jobs :\n" + plan)


def test_le_plan_reste_bon_avec_un_org_id_explicite(base_avec_historique, monkeypatch):
    """Le filtre `(%s::bigint IS NULL OR org_id = %s)` est la forme RÉELLE de
    l'appel côté worker d'org (pas seulement le worker plateforme, org_id=NULL)."""
    sql, params = _requete_emise(monkeypatch, 2)
    plan = _plan(base_avec_historique, sql, params)
    assert "idx_runner_jobs_live" in plan, plan
    assert "Seq Scan on runner_jobs" not in plan, plan


@pytest.mark.parametrize("org_id", [None, 2])
def test_la_charge_en_vol_se_compte_UNE_fois_par_reservation(
        base_avec_historique, monkeypatch, org_id):
    """Sous la rafale (600 travaux dus), chaque lecture de `runner_jobs` ne
    s'exécute qu'UNE fois. La sous-requête corrélée de la première écriture de la
    part équitable s'exécutait 300 à 600 fois (une par ligne éligible, chacune
    recomptant les lignes vivantes de son org) : coût ~ dus × vivants, par org."""
    sql, params = _requete_emise(monkeypatch, org_id)
    lectures = _executions_de_runner_jobs(base_avec_historique, sql, params)
    assert lectures, "le plan ne lit pas runner_jobs ?"
    assert all(boucles <= 1 for _, boucles in lectures), (
        "une lecture de runner_jobs se répète par ligne candidate "
        "(sous-requête corrélée ?) : " + repr(lectures))


def test_les_deux_balayages_du_meme_sondage_en_profitent_aussi(
        base_avec_historique):
    """Épaves (`status='claimed' AND lease_until < NOW()`) et périmés
    (`status='pending' AND …`) — leurs prédicats IMPLIQUENT le prédicat partiel
    de l'index sans qu'aucune réécriture n'ait été nécessaire sur ces deux
    requêtes-là (oto_mcp/db/runner_jobs.py, les deux UPDATE juste avant le CTE
    de réservation)."""
    epaves = _plan(
        base_avec_historique,
        "SELECT id FROM runner_jobs WHERE (%(org)s::bigint IS NULL "
        "OR org_id = %(org)s) AND status = 'claimed' AND lease_until < NOW() "
        "AND attempts >= max_attempts",
        {"org": None})
    assert "idx_runner_jobs_live" in epaves, epaves
    assert "Seq Scan" not in epaves, epaves

    perimes = _plan(
        base_avec_historique,
        "SELECT id FROM runner_jobs WHERE (%(org)s::bigint IS NULL "
        "OR org_id = %(org)s) AND status = 'pending' "
        "AND payload ? '_perime_apres_s'",
        {"org": None})
    assert "idx_runner_jobs_live" in perimes, perimes
    assert "Seq Scan" not in perimes, perimes


def test_l_ancien_index_a_disparu(base_avec_historique):
    """`idx_runner_jobs_claim` est redondant (son prédicat est un sous-ensemble
    de celui du nouveau) — le garder doublerait la maintenance de l'index à
    chaque écriture pour rien."""
    import psycopg
    with psycopg.connect(base_avec_historique) as c:
        noms = {r[0] for r in c.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'runner_jobs'"
        ).fetchall()}
    assert "idx_runner_jobs_live" in noms, noms
    assert "idx_runner_jobs_claim" not in noms, noms

"""Part équitable au claim (27/10/2026) : un worker de plateforme sert d'abord l'org qui a
le MOINS de travaux en vol, puis le plus ancien dû. Une org qui enfile une rafale ne retient
plus les autres derrière elle ; seule à attendre, elle garde toutes les places. En SQL,
donc sur vraie base.

⚠️ Un claim de PLATEFORME prend le travail de n'importe quelle org : chaque banc utilise
des orgs qui lui sont propres et vide leur file avant de conclure.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_claim_equitable_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _enfiler(org):
    from oto_mcp import db
    return db.enqueue_job(org, "start", payload={"input": "go", "tools": []})["id"]


def _prendre(*orgs):
    from oto_mcp import db
    return db.claim_next_job(None, "w-parc", lease_seconds=600, org_ids=list(orgs))


def _vider(*orgs):
    while _prendre(*orgs):
        pass


def _bail_mort(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET lease_until = NOW() - interval '1 minute' "
                     "WHERE id = %s", (job_id,))


def test_une_rafale_ne_retient_pas_l_autre_org(live):
    rafale = [_enfiler(9301) for _ in range(3)]   # les plus anciens
    seul = _enfiler(9302)
    premier = _prendre(9301, 9302)
    assert premier["id"] == rafale[0]              # à égalité : le plus ancien dû
    # 9301 a un travail en vol, 9302 aucun : 9302 passe devant le reste de la rafale.
    assert _prendre(9301, 9302)["id"] == seul
    assert [_prendre(9301, 9302)["id"] for _ in range(2)] == rafale[1:]
    assert _prendre(9301, 9302) is None


def test_seule_a_attendre_une_org_prend_toutes_les_places(live):
    ids = [_enfiler(9303) for _ in range(4)]
    assert [_prendre(9303)["id"] for _ in range(4)] == ids
    assert _prendre(9303) is None


def test_les_orgs_prennent_leur_tour(live):
    a = [_enfiler(9304) for _ in range(3)]
    b = [_enfiler(9305) for _ in range(3)]
    pris = [_prendre(9304, 9305)["id"] for _ in range(6)]
    assert pris == [a[0], b[0], a[1], b[1], a[2], b[2]]


def test_un_bail_mort_ne_compte_pas_comme_en_vol(live):
    tenu = _enfiler(9306)
    assert _prendre(9306)["id"] == tenu
    _bail_mort(tenu)                 # son worker est mort : rien ne tourne pour 9306
    plus_ancien = _enfiler(9306)
    _enfiler(9307)
    # 9306 n'a rien de VIVANT en vol : à égalité, le plus ancien dû (le bail mort,
    # repris, puis son travail suivant) passe d'abord, comme avant.
    assert _prendre(9306, 9307)["id"] == tenu
    assert _prendre(9306, 9307)["org_id"] == 9307
    assert _prendre(9306, 9307)["id"] == plus_ancien
    _vider(9306, 9307)


# ── La part se compte PAR POOL (famille), et sous concurrence ─────────────────

_ABO = "claude_subscription"


def _en_vol_direct(org, famille, n=1):
    """`n` travaux EN VOL (bail vivant) de `famille` pour `org`, posés en base : la
    charge qu'une org tient déjà sur un pool."""
    import json

    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        for _ in range(n):
            conn.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, payload, due_at, "
                "attempts, max_attempts, lease_until, claimed_by) "
                "VALUES (%s, 'start', 'claimed', %s::jsonb, NOW() - interval '1 hour', "
                "1, 3, NOW() + interval '10 minutes', 'w-ailleurs')",
                (org, json.dumps({"model_family": famille})))


def _clore_en_vol(*orgs):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = 'done', finished_at = NOW() "
                     "WHERE org_id = ANY(%s) AND claimed_by = 'w-ailleurs'", (list(orgs),))


def test_la_charge_se_compte_PAR_POOL_pas_par_org(live):
    """A tient deux places sur les ABONNEMENTS de ses membres ; sur le pool `anthropic`
    elle n'a rien en vol. B y a un travail en vol. Sur ce pool, A passe devant B, même
    si le travail de B est plus ancien : compter par org seule pénaliserait A sur un
    pool où elle ne prend rien."""
    from oto_mcp import db
    a, b = 9312, 9313
    _en_vol_direct(a, _ABO, n=2)
    _en_vol_direct(b, "anthropic", n=1)
    de_b = db.enqueue_job(b, "start", payload={"input": "go", "model_family": "anthropic"})["id"]
    de_a = db.enqueue_job(a, "start", payload={"input": "go", "model_family": "anthropic"})["id"]

    def _prise():
        return db.claim_next_job(None, "w-cle", lease_seconds=600, depot="anthropic",
                                 org_ids=[a, b])

    assert _prise()["id"] == de_a, "A n'a rien en vol SUR CE POOL : elle passe d'abord"
    assert _prise()["id"] == de_b
    assert _prise() is None
    _clore_en_vol(a, b)


def test_un_du_FUTUR_reste_exclu_meme_pour_l_org_la_moins_chargee(live):
    """Le tri ne lève pas le filtre `due_at <= NOW()` : l'org sans rien en vol dont le
    seul travail est dû dans une heure ne passe pas devant un travail dû maintenant."""
    from oto_mcp import db
    a, b = 9314, 9315
    _en_vol_direct(b, None, n=1)
    plus_tard = db.enqueue_job(a, "start", payload={"input": "go"}, delai_s=3600)["id"]
    maintenant = _enfiler(b)
    pris = _prendre(a, b)
    assert pris["id"] == maintenant and pris["id"] != plus_tard
    assert _prendre(a, b) is None, "le travail futur attend son heure"
    _clore_en_vol(a, b)


def test_deux_reservations_CONCURRENTES_ne_prennent_pas_le_meme_travail(live, monkeypatch):
    """La 1re réservation tient sa transaction ouverte (non validée) ; la 2e saute la
    ligne verrouillée (`FOR UPDATE OF rj SKIP LOCKED`) et prend la suivante — jamais la
    même, jamais d'attente."""
    import contextlib

    import psycopg
    from psycopg.rows import dict_row

    from oto_mcp import db
    from oto_mcp.db import runner_jobs as module
    un, deux = _enfiler(9316), _enfiler(9317)

    tenue = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    try:
        @contextlib.contextmanager
        def _sans_valider():
            yield tenue

        monkeypatch.setattr(module, "_connect", _sans_valider)
        premiere = db.claim_next_job(None, "w-un", lease_seconds=600,
                                     org_ids=[9316, 9317])
        monkeypatch.undo()
        assert tenue.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        assert premiere["id"] == un

        seconde = db.claim_next_job(None, "w-deux", lease_seconds=600,
                                    org_ids=[9316, 9317])
        assert seconde["id"] == deux, "la ligne tenue est sautée, pas reprise"
        assert db.claim_next_job(None, "w-deux", lease_seconds=600,
                                 org_ids=[9316, 9317]) is None
    finally:
        tenue.rollback()
        tenue.close()
    assert _prendre(9316, 9317)["id"] == un, "annulée, la 1re prise rend son travail"
    _vider(9316, 9317)


def _membre_abonne(org, sub):
    """Une personne de `org` dont l'abonnement `_ABO` est ouvert dans cette org."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                     (org, f"Org {org}"))
        conn.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING", (org, sub))
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, _ABO, org))
    return sub


def test_en_ABONNEMENT_la_part_s_applique_et_le_verrou_tient(live):
    """Un worker d'abonnement (jointures du pool, `FOR UPDATE OF rj`) prend selon la
    part : A a deux membres abonnés et les travaux les plus anciens, B un seul plus
    récent. Après la 1re prise de A, B passe devant le 2d travail de A."""
    from oto_mcp import db
    a, b = 9318, 9319
    a1, a2 = _membre_abonne(a, "part-a1"), _membre_abonne(a, "part-a2")
    b1 = _membre_abonne(b, "part-b1")

    def _travail(org, sub):
        return db.enqueue_job(org, "start", sub=sub,
                              payload={"procedure": "p", "model": "sub:sonnet",
                                       "model_family": _ABO})["id"]

    de_a1, de_a2, de_b1 = _travail(a, a1), _travail(a, a2), _travail(b, b1)

    def _prise():
        return db.claim_next_job(None, "w-abo", lease_seconds=600, depot=_ABO,
                                 famille_seule=True, org_ids=[a, b])

    assert _prise()["id"] == de_a1
    assert _prise()["id"] == de_b1, "A a un travail en vol sur ce pool, B aucun"
    assert _prise()["id"] == de_a2
    assert _prise() is None
    for job in (de_a1, de_a2, de_b1):
        db.complete_job(job, "w-abo", ok=True)

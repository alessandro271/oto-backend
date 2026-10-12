"""Un MEMBRE ne réserve que SES travaux ; le worker de PLATEFORME, toute la file.

Le travail réservé revient avec un jeton de délégation au nom de SON porteur
(`runner_jobs._delegue`). Tant que le claim d'un membre ne filtrait que l'org, un
membre B réservait le travail de son collègue A et recevait un jeton qui agit au nom
de A. Le filtre vit dans la prise SQL (`claim_next_job(porteur=…)`) : prouvé sur vraie
base, puis par la capacité telle que la route l'appelle.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import ResolvedCtx

ORG = 9611
A, B = "sub-porteur-a", "sub-collegue-b"


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_claim_porteur_" + uuid.uuid4().hex[:8]
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


def _enfiler(sub, org=ORG):
    from oto_mcp import db
    return db.enqueue_job(org, "start", payload={"input": "go", "tools": []},
                          sub=sub)["id"]


def _vider(org=ORG):
    from oto_mcp import db
    while db.claim_next_job(None, "w-vidange", lease_seconds=60, org_ids=[org]):
        pass


def test_un_membre_ne_prend_pas_le_travail_d_un_collegue(live):
    from oto_mcp import db
    _vider()
    de_a = _enfiler(A)
    assert db.claim_next_job(ORG, B, lease_seconds=60, porteur=B) is None, (
        "B a réservé le travail de A : il recevrait un jeton au nom de A")
    pris = db.claim_next_job(ORG, A, lease_seconds=60, porteur=A)
    assert pris and pris["id"] == de_a and pris["sub"] == A
    _vider()


def test_le_filtre_saute_le_travail_d_autrui_sans_le_toucher(live):
    """Le plus ancien est celui de A : B prend le SIEN, et celui de A reste `pending`."""
    from oto_mcp import db
    _vider()
    de_a = _enfiler(A)
    de_b = _enfiler(B)
    pris = db.claim_next_job(ORG, B, lease_seconds=60, porteur=B)
    assert pris and pris["id"] == de_b
    assert db.get_job(de_a, ORG)["status"] == "pending"
    _vider()


def test_un_travail_sans_porteur_n_est_jamais_pris_par_un_membre(live):
    from oto_mcp import db
    _vider()
    _enfiler(None)
    assert db.claim_next_job(ORG, A, lease_seconds=60, porteur=A) is None
    _vider()


def test_le_worker_de_plateforme_prend_toujours_toute_la_file(live):
    from oto_mcp import db
    _vider()
    de_a = _enfiler(A)
    pris = db.claim_next_job(None, "w-plateforme", lease_seconds=60, org_ids=[ORG])
    assert pris and pris["id"] == de_a
    _vider()


def _servi_tel_quel(monkeypatch):
    # Hors sujet ici : la délégation (comptes, rôles) et la clé de modèle.
    monkeypatch.setattr(RJ, "_delegue", lambda job, bail, claimant: job)
    monkeypatch.setattr(RJ, "_avec_cle", lambda job, *a, **k: job)


def test_la_capacite_filtre_le_membre_par_son_porteur(live, monkeypatch):
    _vider()
    _servi_tel_quel(monkeypatch)
    de_a = _enfiler(A)
    claim = RJ.JobsInput(op="claim")
    assert RJ._jobs(ResolvedCtx(sub=B, org_id=ORG), claim) == {"job": None}
    assert RJ._jobs(ResolvedCtx(sub=A, org_id=ORG), claim)["job"]["id"] == de_a
    _vider()


def test_la_capacite_laisse_toute_la_file_au_worker_de_plateforme(live, monkeypatch):
    _vider()
    _servi_tel_quel(monkeypatch)
    de_a = _enfiler(A)
    worker = ResolvedCtx(sub="w-plateforme", org_id=None, platform_worker=True)
    pris = RJ._jobs(worker, RJ.JobsInput(op="claim", org_ids=[ORG]))["job"]
    assert pris["id"] == de_a
    _vider()


def test_la_capacite_passe_le_porteur_aux_deux_prises(monkeypatch):
    """Les DEUX appels à `claim_next_job` (avant et après la production de campagne)
    portent le filtre : la campagne fabrique des travaux au nom de son déclarant."""
    vus = []
    monkeypatch.setattr(RJ.db, "claim_next_job",
                        lambda *a, **k: vus.append(k.get("porteur")) or None)
    monkeypatch.setattr(RJ, "_produire_pour_une_campagne", lambda *a, **k: None)
    RJ._jobs(ResolvedCtx(sub=B, org_id=ORG), RJ.JobsInput(op="claim"))
    assert vus == [B, B]
    vus.clear()
    RJ._jobs(ResolvedCtx(sub="w", org_id=None, platform_worker=True),
             RJ.JobsInput(op="claim"))
    assert vus == [None, None]

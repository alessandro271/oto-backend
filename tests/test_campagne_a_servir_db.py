"""La production de travail au sondage, exercée en SQL RÉEL.

⚠️ Ce fichier existe à cause d'un défaut que rien ne pouvait voir. Tous les
tests de capacité (`test_runner_jobs.py`) remplacent `db.campagne_a_servir` par
un mock : ils prouvent ce que la capacité fait du RÉSULTAT, jamais que la
requête s'exécute. Le 07/09/2026, elle ne s'exécutait pas une seule fois —
`pg_try_advisory_xact_lock(%s, f.id)` sur une colonne BIGSERIAL levait
`UndefinedFunction` (Postgres n'offre que `(bigint)` ou `(int, int)`), le `try`
de `_produire_pour_une_campagne` l'avalait, et le sondage rendait « aucun
travail » pour toujours. Trouvé en lisant le journal d'un canari, pas un banc.

La leçon, plus large que ce bug : **un chemin qui a un repli doit être exercé
là où il s'exécute vraiment**, sinon le repli rend le banc aveugle. Patron de
base éphémère repris de `test_account_group_grants_db.py`.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_campagne_" + uuid.uuid4().hex[:8]
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


@pytest.fixture
def org_neuve(live):
    """Une org et son membre, uniques : la base est module-scope et survit d'un
    test à l'autre."""
    from oto_mcp import db, org_store
    uniq = uuid.uuid4().hex[:8]
    sub = f"campagne_{uniq}"
    db.upsert_user(sub)
    return {"org": org_store.create_org(f"org_{uniq}", created_by=sub), "sub": sub}


def _flotte(org: int, sub: str, **surcharges):
    from oto_mcp import db
    champs = dict(label="essai", procedure="fleet-demo", tools=["data_rows"],
                  namespace="t", max_rows=None)
    champs.update(surcharges)
    return db.create_fleet(org, sub, **champs)


def test_campagne_armee_est_servie(org_neuve):
    """Le cas nominal — celui qui ne s'est JAMAIS produit en production.

    C'est le test qui manquait : il exécute la requête, donc il aurait échoué
    sur `UndefinedFunction` au lieu de laisser passer un sondage muet."""
    from oto_mcp import db
    f = _flotte(org_neuve["org"], org_neuve["sub"])
    db.armer(f["id"], org_neuve["org"], rows_at_launch=2)

    servie = db.campagne_a_servir(org_neuve["org"])

    assert servie is not None, "une campagne armée doit être servie au sondage"
    assert servie["id"] == f["id"]


def test_campagne_en_brouillon_nest_pas_servie(org_neuve):
    """Le pendant : sans armement, rien ne sort. Il tient la garde du haut —
    un `draft` qui se mettrait à produire du travail serait pire que le bug."""
    from oto_mcp import db
    _flotte(org_neuve["org"], org_neuve["sub"])
    assert db.campagne_a_servir(org_neuve["org"]) is None


def test_campagne_au_plafond_de_lignes_nest_plus_servie(org_neuve):
    """`max_rows` atteint : la campagne cesse d'être servie, sans être arrêtée.
    Exercé en SQL parce que le compte vit dans une sous-requête corrélée."""
    from oto_mcp import db
    f = _flotte(org_neuve["org"], org_neuve["sub"], max_rows=1)
    db.armer(f["id"], org_neuve["org"], rows_at_launch=1)
    db.enqueue_job(org_neuve["org"], "start", fleet_id=f["id"], sub=org_neuve["sub"])

    assert db.campagne_a_servir(org_neuve["org"]) is None, (
        "au plafond de lignes, plus aucun travail ne doit être produit")


def test_arreter_campagnes_epuisees_sexecute(org_neuve):
    """L'autre requête du même chemin, appelée AVANT de servir. Elle est mockée
    partout ailleurs ; ici on vérifie seulement qu'elle s'exécute contre le vrai
    schéma et rend une liste — le défaut du jour était de cette nature."""
    from oto_mcp import db
    f = _flotte(org_neuve["org"], org_neuve["sub"], max_consecutive_failures=2)
    db.armer(f["id"], org_neuve["org"])

    arretees = db.arreter_campagnes_epuisees(org_neuve["org"])

    assert isinstance(arretees, list)
    assert f["id"] not in arretees, "aucun échec : rien à arrêter"

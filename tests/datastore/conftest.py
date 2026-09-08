"""Le harnais partagé des bancs du datastore : une base réelle, jetable, par module.

⚠️ **Il vit ici et pas dans un module de test importé par les autres.** Deux bancs
oto#140 ont commencé par `from tests.datastore.test_… import live` : ça passe en local,
où la racine du dépôt est dans `sys.path`, et ça tombe en CI —
`ModuleNotFoundError: No module named 'tests'`, parce qu'il n'y a pas de `__init__.py`.
Le banc était vert sur ma machine et rouge sur le tronc, sans qu'aucune assertion ne
soit en cause.

Aucun autre fichier du dépôt n'importait d'un module de test : c'était le signe qu'il
ne fallait pas commencer. `conftest.py` est le mécanisme prévu — pytest le découvre,
il n'y a rien à importer, et la question de `sys.path` ne se pose jamais.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base PostgreSQL neuve pour le module, détruite à la sortie.

    Une vraie base plutôt qu'un double : ce qu'on vérifie ici, c'est ce que le
    STOCKAGE porte — un simulacre rendrait ce qu'on lui a appris à rendre.
    """
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_org_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()

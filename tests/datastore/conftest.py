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

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    """Le schéma réel sur la base neuve du module (`pg_module_dsn`), pointé par
    `DATABASE_URL`.

    Une vraie base plutôt qu'un double : ce qu'on vérifie ici, c'est ce que le
    STOCKAGE porte — un simulacre rendrait ce qu'on lui a appris à rendre.

    La création et la destruction de la base vivent une étape plus haut, dans la
    fixture partagée du `conftest` racine : ce harnais-ci n'est plus que le
    branchement du code sur cette base.
    """
    pytest.importorskip("psycopg")

    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant

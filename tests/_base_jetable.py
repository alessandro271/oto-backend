"""Une base PostgreSQL VIDE à soi, pour les bancs qui jouent la naissance d'une base (#969).

`pg_module_dsn` (conftest) rend une base par MODULE ; un banc de naissance en veut une
par TEST — chacun part d'une base réellement vide, ou d'un état qu'il construit. La
base est pointée par `DATABASE_URL`, le pool de `oto_mcp.db._conn` remis à neuf (sans
quoi on parlerait à la base d'un voisin), puis tout est rendu et la base détruite.

`role=(nom, mot_de_passe)` : le démarrage parle à la base sous CE rôle (déjà créé par
l'appelant) plutôt que sous celui du serveur de test — pour jouer une base dont le
rôle n'a pas tous les droits.

Ce module n'est pas collecté (pas de préfixe `test_`) : `from _base_jetable import …`.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import psycopg


def _dsn(pg_dsn: str, base: str, role: Optional[tuple[str, str]] = None) -> str:
    u = urlsplit(pg_dsn)
    netloc = u.netloc
    if role is not None:
        netloc = f"{role[0]}:{role[1]}@{u.netloc.rsplit('@', 1)[-1]}"
    return urlunsplit((u.scheme, netloc, "/" + base, u.query, u.fragment))


@contextmanager
def base_jetable(pg_dsn: str, role: Optional[tuple[str, str]] = None):
    """Rend un ouvreur de connexion (autocommit, rôle du serveur de test) sur la base."""
    from oto_mcp.db import _conn as dbconn

    nom = "oto_naissance_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = _dsn(pg_dsn, nom, role)
    dbconn._pool = None
    try:
        yield lambda: psycopg.connect(_dsn(pg_dsn, nom), autocommit=True)
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

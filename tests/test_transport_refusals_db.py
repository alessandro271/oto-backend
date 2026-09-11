"""La lecture du compteur de refus, contre une VRAIE base.

Le classement des causes est éprouvé ailleurs (`test_transport_refusals.py`, contre un
vrai transport). Ici on éprouve l'autre moitié, celle qui reste muette tant qu'on ne
l'exécute pas : le SQL de la lentille. Il découpe la cause dans `tool` et lit
l'environnement dans le JSON des arguments — deux choses qu'aucun banc en mémoire ne
peut faire échouer, et qui ne rendraient un résultat faux qu'en production.

⚠️ Base NEUVE par module (`pg_module_dsn`) : `pg_dsn` est une base FIXE partagée par
toutes les sessions de ce poste, y écrire est écrire chez les voisines.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def base(pg_module_dsn):
    pytest.importorskip("psycopg")
    avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = avant


def _refus(cause: str, statut: int, env: str, **extra) -> dict:
    args = {"statut": statut, "methode": "POST", "env": env, **extra}
    return {"server": "oto", "kind": "transport", "tool": f"refus:{cause}",
            "args": args, "ok": False}


def test_la_lentille_ventile_par_cause_par_statut_et_par_environnement(base):
    """Les deux environnements écrivent dans la MÊME base : sans l'axe `env`, les
    deux trafics s'additionneraient en silence et on croirait mesurer la production."""
    from oto_mcp import db

    for ligne in (
        _refus("version_refusee", 400, "production", version_demandee="2019-01-01"),
        _refus("version_refusee", 400, "production", version_demandee="2019-01-01"),
        _refus("version_refusee", 400, "canari", version_demandee="2024-11-05"),
        _refus("session_absente", 400, "production"),
        _refus("session_inconnue", 404, "production"),
    ):
        db.insert_tool_call(ligne)
    # Une ligne d'OUTIL du même jour : elle ne doit pas entrer dans ce compte.
    db.insert_tool_call({"server": "oto", "kind": "mcp", "tool": "oto_whoami",
                         "sub": "u-1", "args": {}, "ok": True})

    out = db.transport_refusal_stats(since_days=7)
    assert out["total"] == 5
    par_cle = {(r["env"], r["cause"], r["statut"]): int(r["n"]) for r in out["by_cause"]}
    assert par_cle == {
        ("production", "version_refusee", 400): 2,
        ("canari", "version_refusee", 400): 1,
        ("production", "session_absente", 400): 1,
        ("production", "session_inconnue", 404): 1,
    }
    # Deux causes distinctes derrière le MÊME code HTTP : c'est ce que le journal
    # d'accès ne sait pas faire, et la seule raison d'être de ce compteur.
    assert len({c for _, c, s in par_cle if s == 400}) == 2

    versions = {(r["env"], r["version_demandee"]): int(r["n"])
                for r in out["protocol_versions_refused"]}
    assert versions == {("production", "2019-01-01"): 2, ("canari", "2024-11-05"): 1}


def test_une_fenetre_hors_bornes_est_ecretee_et_non_refusee(base):
    """`days=0` ferait une requête vide et `days=100000` un balayage complet : la
    fenêtre est écrêtée, et la réponse DIT celle qui a servi — sinon on lirait un
    chiffre sans savoir sur quoi il porte.

    (Ce module partage sa base entre ses tests : les lignes du test précédent sont
    encore là. On éprouve donc l'écrêtage et la forme, pas un compte.)"""
    from oto_mcp import db
    assert db.transport_refusal_stats(since_days=0)["since_days"] == 1
    assert db.transport_refusal_stats(since_days=100000)["since_days"] == 365
    out = db.transport_refusal_stats(since_days=7)
    assert isinstance(out["total"], int) and isinstance(out["by_cause"], list)

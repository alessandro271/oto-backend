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


def test_la_mesure_du_debit_se_lit_par_environnement_classe_et_mode(base):
    """La limite de débit (F-025) écrit une ligne par classe et par minute
    (`kind='debit'`) ; la lentille les somme. Les lignes de la vraie fabrique, pas une
    imitation : c'est leur forme que le SQL découpe."""
    from oto_mcp import db
    from oto_mcp import limite_debit_mesure as mesure

    def minute(env: str, mode: str, n: int, **compte):
        os.environ["OTO_SENTRY_ENV"] = env
        m = mesure.Mesure(mode)
        m.basculer(n, {})
        for issue, fois in compte.items():
            for _ in range(fois):
                m.noter_decision("anonyme", issue)
        m.noter_usage("anonyme", 40.0 + n % 10)
        return m.basculer(n + 1, {"anonyme": 600.0})

    avant = os.environ.get("OTO_SENTRY_ENV")
    try:
        lignes = (minute("production", "report", 1001, allowed=5, would_refuse=2)
                  + minute("production", "report", 1002, allowed=3, would_refuse=1)
                  + minute("canari", "enforce", 1003, allowed=1, refused=4))
    finally:
        if avant is None:
            os.environ.pop("OTO_SENTRY_ENV", None)
        else:
            os.environ["OTO_SENTRY_ENV"] = avant
    mesure.publier(lignes)

    out = db.transport_refusal_stats(since_days=7)
    par_cle = {(r["env"], r["classe"], r["mode"]): r for r in out["rate_limit"]}
    prod = par_cle[("production", "anonyme", "report")]
    assert (prod["minutes"], prod["allowed"], prod["would_refuse"], prod["refused"]) \
        == (2, 8, 3, 0)
    assert prod["peak_pct"] == 42.0 and prod["limit_per_minute"] == 600
    canari = par_cle[("canari", "anonyme", "enforce")]
    assert (canari["allowed"], canari["refused"]) == (1, 4)
    # la mesure n'entre pas dans le compte des refus du transport
    assert all(not r["cause"].startswith("anonyme") for r in out["by_cause"])

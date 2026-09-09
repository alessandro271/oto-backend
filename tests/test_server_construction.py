"""Construire n'est pas démarrer.

`oto_mcp/server.py` construisait son instance AU NIVEAU MODULE. Un import — donc
la collecte de tout test qui touche ce module — préparait la base et allait
chercher les catalogues fédérés chez des tiers. Deux attentes sans délai maximal
propre dans un chemin d'import (oto-backend#892), et plusieurs secondes payées
par quiconque importe le module pour tout autre chose.

Ce banc fige la séparation : l'import définit, `main()` démarre.

⚠️ Ce qu'il ne couvre PAS, et pourquoi : le chantier d'origine mesurait aussi
l'ordre des étapes de préparation et leur reprise après échec, en s'appuyant sur
le moteur de migrations versionnées. Ce moteur n'est pas intégré (oto-backend#891)
et la préparation reste **fail-open** — un backfill qui casse ne doit pas empêcher
le serveur de répondre. Ces deux tests-là mesuraient donc un contrat qui n'existe
pas ici ; ils sont laissés de côté plutôt que maquillés.
"""
import importlib

import pytest


def test_l_import_ne_construit_rien_et_ne_touche_NI_la_base_NI_le_reseau(monkeypatch):
    """La version d'avant n'espionnait que `init_db` et `FastMCP.__init__` : elle
    serait restée verte si l'import avait ouvert une connexion par un autre
    chemin. On surveille donc la connexion elle-même.

    ⚠️ Le témoin « montage distant » a disparu le 2026-09-09 avec la fédération MCP
    (ADR 0069) : plus rien, au montage, n'attend un tiers."""
    from oto_mcp import db, server
    from oto_mcp.db import _conn
    vu = []
    monkeypatch.setattr(db, "init_db", lambda: vu.append("init_db"))
    monkeypatch.setattr(_conn, "_connect", lambda *a, **k: vu.append("connexion"))
    # ⚠️ On NE remplace PAS `FastMCP.__init__` : un faux constructeur rend un
    # objet inutilisable, et l'échec sort alors en `AttributeError` interne à
    # FastMCP — un rouge illisible, qui n'apprend rien à qui le découvre. Le
    # témoin qu'une instance a été construite à l'import, c'est `server.mcp`.
    try:
        importlib.reload(server)
        assert server.mcp is None, (
            "une instance a été construite AU NIVEAU MODULE : l'import prépare la "
            "base, cf. oto-backend#892")
        assert vu == [], f"l'import travaille : {vu}"
    finally:
        # `reload` a réinitialisé les globales du module (`_PREPARED`, les
        # instructions rendues) : on rend au reste de la session un module dans
        # l'état où on l'a trouvé, sinon ce banc en fait payer le prix aux autres.
        importlib.reload(server)


def test_construire_ne_prepare_pas_la_base(monkeypatch):
    """Construire n'est pas démarrer : `_build_mcp` monte le catalogue et n'appelle
    personne. `main()` seul prépare la base.

    ⚠️ Ce banc s'appelait `test_le_catalogue_distant_ne_part_QUE_sur_demande` et
    gardait le paramètre `include_mounts` : il n'a plus d'objet depuis le 2026-09-09
    (ADR 0069). Ce qu'il reste à garder — et qui n'a jamais dépendu de la fédération —
    c'est qu'une construction ne touche pas la base."""
    from oto_mcp import server
    vu = []
    monkeypatch.setattr(server, "_prepare_database", lambda: vu.append("base"))

    server._build_mcp("noauth")
    assert vu == [], "construire ne prépare pas la base et n'appelle personne"


def test_la_preparation_reste_gardee_par_process(monkeypatch):
    """Elle a changé d'appelant, pas de nature : toujours une fois par process."""
    from oto_mcp import server
    monkeypatch.setattr(server, "_PREPARED", False)
    tours = []
    monkeypatch.setattr(server.db, "init_db", lambda: tours.append(1))
    monkeypatch.setattr(server, "instructions", type("_", (), {
        "seed_platform_blocks": staticmethod(lambda: None)})())
    server._prepare_database()
    server._prepare_database()
    assert tours == [1], "deux appels, une seule préparation"

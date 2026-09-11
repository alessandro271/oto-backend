"""oto#166, barreau 1 — la trace : QUI a posé une installation, et le retrait du membre.

ADR 0050 §E7. Avant ce lot, `user_selected_connectors` n'avait pas de provenance et un
retrait EFFAÇAIT la ligne : une ligne semée, posée par le kit ou choisie par le membre
étaient indiscernables, et « activer pour toute l'org » réinstallait chez un membre ce
qu'il venait de retirer — contre la promesse de sa propre description. Les deux
décisions du 11/09 qui suivent (Q1 : un retrait du kit ne retire que ce que le kit a
posé ; Q2 : la poussée ne défait jamais un retrait) sont intenables sans cette trace.

Ce banc éprouve le comportement SERVI, pas la déclaration : les gestes passent par la
vraie chaîne de l'adaptateur REST (`_datastore_rest.call`), la carte se lit sur
`GET /api/me/connectors`, le semis par le vrai `compute_hidden_tools` — sur un vrai
PostgreSQL bootté par `init_db()`, où la PK et l'`ALTER` de la base partagée jouent.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from _datastore_rest import call, stub_authz


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        from oto_mcp.connectors import activation
        init_db()
        for nom in ("hunter", "kaspr", "folk"):
            activation.set_activation(nom, True)       # master plateforme ON
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _org(nom: str, admin: str, *membres: str) -> int:
    from oto_mcp import org_store
    oid = org_store.create_org(nom, created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, m, "org_member")
    return oid


def _ligne(sub: str, org: int, connecteur: str):
    from oto_mcp import db
    with db._connect() as c:
        return c.execute(
            "SELECT state, origin FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org, connecteur)).fetchone()


def _retrait(sub: str, org: int, connecteur: str):
    from oto_mcp import db
    with db._connect() as c:
        return c.execute(
            "SELECT removed_at FROM connector_selection_removed "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org, connecteur)).fetchone()


def _carte(monkeypatch, sub: str, org: int, connecteur: str) -> dict:
    """La ligne SERVIE par `GET /api/me/connectors?name=…` — ce que lit le front."""
    stub_authz(monkeypatch, org_id=org)
    code, corps = call("connectors.me", sub=sub, query=f"name={connecteur}".encode())
    assert code == 200, corps
    (ligne,) = corps["connectors"]
    return ligne


def _geste(monkeypatch, cle: str, sub: str, org: int, connecteur: str) -> tuple[int, dict]:
    stub_authz(monkeypatch, org_id=org)
    return call(cle, sub=sub, path_params={"name": connecteur})


# ── le schéma : la colonne de la base partagée, et la table des retraits ─────────

def test_le_schema_porte_la_provenance_et_les_retraits(live):
    from oto_mcp import db
    with db._connect() as c:
        col = c.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'user_selected_connectors' AND column_name = 'origin'"
        ).fetchone()
        pk = c.execute(
            "SELECT string_agg(a.attname, ',' ORDER BY k.n) AS cols "
            "FROM pg_index i JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, n) ON true "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "WHERE i.indrelid = 'connector_selection_removed'::regclass AND i.indisprimary"
        ).fetchone()
    # NOT NULL + défaut constant : ce que l'ancien code (qui n'écrit pas la colonne)
    # pose, c'est `inconnue` — la seule valeur qu'aucun geste d'org ne retire.
    assert col is not None and col["is_nullable"] == "NO"
    assert col["column_default"].startswith("'inconnue'")
    assert pk["cols"] == "sub,org_id,connector"


def test_la_base_partagee_recoit_la_colonne_par_l_alter(live):
    """La production n'a PAS la colonne : son `CREATE TABLE IF NOT EXISTS` est sauté,
    seul l'`ALTER` de `_init.py` la pose. Une base neuve la reçoit inline, donc aucun
    autre banc ne rougirait si l'`ALTER` disparaissait. On remet la table dans l'état
    d'avant le lot, avec une ligne, et on reboote."""
    from oto_mcp import db
    from oto_mcp.db import init_db
    org = _org("Kit166 alter", "u166-alter")
    with db._connect() as c:
        c.execute("ALTER TABLE user_selected_connectors DROP COLUMN origin")
        c.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                  "VALUES ('u166-alter', %s, 'folk', 'active')", (org,))
    init_db()
    # La ligne d'avant la trace se lit `inconnue` — jamais retirée par un geste d'org.
    assert _ligne("u166-alter", org, "folk")["origin"] == "inconnue"
    test_le_schema_porte_la_provenance_et_les_retraits(live)


def test_une_ecriture_de_l_ancien_code_se_lit_inconnue(live):
    """La base est partagée : la production sert encore l'ancien code quand cet ALTER
    s'applique. Son INSERT — reproduit à la lettre — ne nomme pas la colonne."""
    from oto_mcp import db
    org = _org("Kit166 ancien", "u166-anc-admin")
    with db._connect() as c:
        c.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                  "VALUES (%s, %s, %s, %s) ON CONFLICT (sub, org_id, connector) "
                  "DO UPDATE SET state = EXCLUDED.state, selected_at = NOW()",
                  ("u166-anc-admin", org, "hunter", "active"))
    assert _ligne("u166-anc-admin", org, "hunter")["origin"] == "inconnue"


# ── les gestes du MEMBRE ─────────────────────────────────────────────────────────

def test_installer_soi_meme_trace_membre_et_la_carte_le_dit(live, monkeypatch):
    sub = "u166-sel"
    org = _org("Kit166 select", "u166-sel-admin", sub)
    code, corps = _geste(monkeypatch, "connectors.select", sub, org, "hunter")
    assert code == 200, corps
    assert _ligne(sub, org, "hunter")["origin"] == "membre"
    carte = _carte(monkeypatch, sub, org, "hunter")
    assert carte["state"] == "active" and carte["origin"] == "membre"
    assert "removed_at" not in carte


def test_une_pause_garde_la_provenance_une_reprise_la_passe_a_membre(live, monkeypatch):
    """E7 : mettre en pause ce que le kit a posé ne se l'approprie pas (un retrait du
    kit l'ôtera encore) ; le REPRENDRE, si — c'est désormais son choix."""
    from oto_mcp import db
    from oto_mcp.connectors import selection as sel
    sub = "u166-pause"
    org = _org("Kit166 pause", "u166-pause-admin", sub)
    with db._connect() as c:
        assert sel.install_for_member(c, sub, "kaspr", org, sel.KIT) == "installed"
    assert _geste(monkeypatch, "connectors.pause", sub, org, "kaspr")[0] == 200
    assert dict(_ligne(sub, org, "kaspr")) == {"state": "paused", "origin": "kit"}
    assert _carte(monkeypatch, sub, org, "kaspr")["origin"] == "kit"
    assert _geste(monkeypatch, "connectors.select", sub, org, "kaspr")[0] == 200
    assert dict(_ligne(sub, org, "kaspr")) == {"state": "active", "origin": "membre"}


def test_le_retrait_du_membre_est_retenu_avec_sa_date(live, monkeypatch):
    sub = "u166-retrait"
    org = _org("Kit166 retrait", "u166-retrait-admin", sub)
    assert _geste(monkeypatch, "connectors.select", sub, org, "folk")[0] == 200
    code, corps = _geste(monkeypatch, "connectors.unselect", sub, org, "folk")
    assert code == 200 and corps["removed"] is True
    assert _ligne(sub, org, "folk") is None          # la sélection est bien retirée…
    trace = _retrait(sub, org, "folk")
    assert trace is not None                          # … et le « non » est RETENU
    carte = _carte(monkeypatch, sub, org, "folk")
    assert carte["state"] == "not_selected"
    assert carte["removed_at"] == trace["removed_at"]   # même fabrique de lignes : une chaîne
    # Le réinstaller soi-même efface le retrait : son dernier geste n'en est plus un.
    assert _geste(monkeypatch, "connectors.select", sub, org, "folk")[0] == 200
    assert _retrait(sub, org, "folk") is None
    assert "removed_at" not in _carte(monkeypatch, sub, org, "folk")


def test_un_retrait_qui_ne_trouve_rien_n_ecrit_rien(live, monkeypatch):
    sub = "u166-rien"
    org = _org("Kit166 rien", "u166-rien-admin", sub)
    code, corps = _geste(monkeypatch, "connectors.unselect", sub, org, "hunter")
    assert code == 404 and corps["error"] == "connector_not_selected"
    assert _retrait(sub, org, "hunter") is None


# ── un geste d'ORG ne défait pas le retrait (le défaut lu au tag v1.264.0) ────────

def test_activer_pour_toute_l_org_trace_kit_et_respecte_le_retrait(live, monkeypatch):
    admin, a_retire, neuf = "u166-bulk-admin", "u166-bulk-retire", "u166-bulk-neuf"
    org = _org("Kit166 bulk", admin, a_retire, neuf)
    assert _geste(monkeypatch, "connectors.select", a_retire, org, "hunter")[0] == 200
    assert _geste(monkeypatch, "connectors.unselect", a_retire, org, "hunter")[0] == 200
    stub_authz(monkeypatch, org_id=org)
    code, corps = call("connectors.bulk_select", sub=admin,
                       path_params={"id": str(org), "name": "hunter"})
    assert code == 200, corps
    # Au tag servi, `state_of is None` réinstallait chez qui venait de retirer.
    assert _ligne(a_retire, org, "hunter") is None
    assert _ligne(neuf, org, "hunter")["origin"] == "kit"
    assert _ligne(admin, org, "hunter")["origin"] == "kit"
    assert corps["activated"] == 2 and corps["skipped"] == 1


# ── le semis : la provenance, et jamais par-dessus un retrait ─────────────────────

class _Ctx:
    class _FastMCP:
        async def list_tools(self, run_middleware=False):
            return [type("T", (), {"name": n})()
                    for n in ("hunter_x", "kaspr_x", "folk_x", "oto_whoami")]

    fastmcp = _FastMCP()


def test_le_semis_trace_kit_et_ne_remet_pas_un_retrait(live, monkeypatch):
    """Un geste du kit a pu poser la ligne AVANT le premier passage du membre, et il a
    pu la retirer depuis l'écran, qui ne sème pas : son premier handshake ne doit pas
    la lui remettre (E6), et ce qu'il reçoit porte la provenance `kit` (E7)."""
    from oto_mcp import org_store, session_visibility as SV
    sub = "u166-semis"
    org = _org("Kit166 semis", "u166-semis-admin", sub)
    org_store.set_org_default_connectors(org, ["hunter", "kaspr"])
    from oto_mcp import db
    with db._connect() as c:
        c.execute("INSERT INTO connector_selection_removed (sub, org_id, connector) "
                  "VALUES (%s, %s, 'kaspr')", (sub, org))
    caches = asyncio.run(SV.compute_hidden_tools(_Ctx(), sub, org=org))
    assert _ligne(sub, org, "hunter")["origin"] == "kit"
    assert _ligne(sub, org, "kaspr") is None
    # Ce que voit l'agent : hunter installé, kaspr retiré, folk jamais au kit.
    assert "hunter_x" not in caches
    assert {"kaspr_x", "folk_x"} <= caches

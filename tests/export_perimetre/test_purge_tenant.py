"""La purge d'un tenant tiers parti sur sa propre instance, sur une vraie base à plusieurs
tenants (`oto_mcp/export_perimetre/purge.py`).

Le banc est celui de l'export (`perimetre_banc`) : chaque ligne semée porte le MARQUEUR
de son propriétaire dans une valeur texte. « Tout est parti, rien d'autrui » se vérifie
donc par un second chemin, sans les règles du classement : après la purge, aucune ligne
d'aucune table ne porte le marqueur du tenant purgé (sauf nos pièces conservées), et
chaque table porte autant de lignes au marqueur du voisin qu'avant.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre import purge  # noqa: E402
from oto_mcp.export_perimetre.perimetre import ComptesHorsTenant  # noqa: E402
from perimetre_banc import A, B, membre, semer  # noqa: E402

C, D = "C5e2a", "D8b4f"


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        semes = {m: semer(c, m) for m in (A, B, C, D)}
        # C partage un projet à un compte du tenant B : un partage vers un tiers.
        c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                  "principal_id, granted_by) VALUES ('project', %s, 'user', %s, %s)",
                  (str(semes[C]["projet"]), semes[B]["alice"], semes[C]["alice"]))
        # D compte dans son org un membre qui n'est pas un compte du tenant.
        membre(c, semes[D]["org"], "compte-hors-tenant")
        yield {"dsn": pg_module_dsn, **semes}


def _connexion(base, lecture_seule=True):
    conn = psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row)
    conn.read_only = lecture_seule
    return conn


def _marques(c, m: str) -> dict[str, int]:
    """Par table, les lignes dont le texte porte le marqueur `m` — hors règles."""
    tables = [r["t"] for r in c.execute(
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")]
    out = {}
    for t in tables:
        n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text LIKE %s",
                      (f"%{m}%",)).fetchone()["n"]
        if n:
            out[t] = n
    return out


def test_une_piece_conservee_qu_une_cascade_emporterait_refuse(base):
    with _connexion(base) as c, pytest.raises(purge.PurgeRefusee) as e:
        purge.a_blanc(c, base[B]["slug"])
    assert e.value.code == "reference_entrante"
    assert "billing_contracts" in str(e.value)


def test_un_partage_vers_un_tiers_refuse(base):
    with _connexion(base, lecture_seule=False) as c:
        c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (base[C]["org"],))
    with _connexion(base) as c, pytest.raises(purge.PurgeRefusee) as e:
        purge.a_blanc(c, base[C]["slug"])
    assert e.value.code == "partage_vers_tiers"
    assert "resource_grants" in str(e.value)


def test_un_tenant_refuse_ne_perd_pas_son_journal(base):
    with _connexion(base) as c:
        avant = _marques(c, C)["tool_calls"]
    with _connexion(base, lecture_seule=False) as c, pytest.raises(purge.PurgeRefusee):
        purge.vider_par_lots(c, base[C]["slug"], lot=1)
    with _connexion(base) as c:
        assert _marques(c, C)["tool_calls"] == avant


def test_un_membre_hors_tenant_refuse(base):
    with _connexion(base) as c, pytest.raises(ComptesHorsTenant):
        purge.a_blanc(c, base[D]["slug"])


def test_le_tenant_primaire_ne_se_purge_pas(base):
    with _connexion(base) as c:
        slug = c.execute("SELECT slug FROM tenants WHERE id = 1").fetchone()["slug"]
        with pytest.raises(purge.PurgeRefusee) as e:
            purge.a_blanc(c, slug)
    assert e.value.code == "tenant_primaire"


def test_appliquer_vide_le_tenant_garde_le_voisin_puis_ne_fait_plus_rien(base):
    with _connexion(base, lecture_seule=False) as c:
        c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (base[A]["org"],))
    with _connexion(base) as c:
        voisin, avant = _marques(c, B), _marques(c, A)
        conserve = {t: n for t, n in avant.items() if t in purge.CONSERVEES}
        plan = purge.a_blanc(c, base[A]["slug"])
        assert _marques(c, A) == avant                     # à blanc : rien d'écrit
    assert plan.id_tenant == base[A]["tenant"]
    assert plan.comptes == 2 and len(plan.orgs) == 2      # l'org et l'org d'inscription
    for t in ("users", "orgs", "tenants", "projects", "docs", "datastore_rows", "nodes",
              "blocks", "tool_calls", "resource_grants", "connector_availability"):
        assert plan.supprimer[t] >= 1, t
    assert plan.supprimer["users"] == 2 and plan.supprimer["tenants"] == 1
    assert set(plan.conserver) == set(purge.CONSERVEES)
    with _connexion(base) as c:
        assert sum(purge.journal_par_mois(c, base[A]["slug"]).values()) == plan.supprimer["tool_calls"]
    with _connexion(base, lecture_seule=False) as c:
        # Le journal d'abord, par tranches d'ids (ici de 2), puis la transaction finale.
        assert purge.vider_par_lots(c, base[A]["slug"], lot=2) == plan.supprimer["tool_calls"]
        assert _marques(c, B) == voisin
        c.autocommit = False
        fait = purge.appliquer(c, base[A]["slug"])
    assert fait.supprimer == {**plan.supprimer, "tool_calls": 0}
    with _connexion(base) as c:
        # Rien du tenant ne reste, hors nos pièces ; le voisin est intact, ligne pour ligne.
        assert _marques(c, A) == conserve
        assert _marques(c, B) == voisin
        assert c.execute("SELECT count(*) AS n FROM tenants WHERE slug = %s",
                         (base[A]["slug"],)).fetchone()["n"] == 0
        # Idempotent : rejoué, le geste ne trouve plus rien.
        assert purge.a_blanc(c, base[A]["slug"]).id_tenant is None
    with _connexion(base, lecture_seule=False) as c:
        c.autocommit = False
        assert purge.appliquer(c, base[A]["slug"]).vide


def test_les_objets_se_relevent_avant_la_purge(base):
    with _connexion(base) as c:
        cles, prefixes = purge.cles_d_objet(c, base[C]["slug"],
                                            "https://stockage-source.exemple.test")
    alice = base[C]["alice"]
    attendues = {k for k in base[C]["objets"] if not k.startswith("images/")}
    assert attendues <= cles
    assert prefixes == {f"images/{alice.replace(':', '%3A')}/",
                        f"images/{base[C]['bob'].replace(':', '%3A')}/"}

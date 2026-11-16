"""La purge d'un tenant tiers parti sur sa propre instance, sur une vraie base à plusieurs
tenants (`oto_mcp/export_perimetre/purge.py`).

Le banc est celui de l'export (`perimetre_banc`) : chaque ligne semée porte le MARQUEUR
de son propriétaire dans une valeur texte. « Tout est parti, rien d'autrui » se vérifie
donc par un second chemin, sans les règles du classement : après la purge, aucune ligne
d'aucune table ne porte le marqueur du tenant purgé (sauf nos pièces conservées et les
lignes du voisin qui le citent), et chaque table porte autant de lignes au marqueur du
voisin qu'avant.

Le voisin B porte les cas croisés : un appel de support (administrateur hors tenant) dans
l'org de A, et le même administrateur dans l'org de B ; un appel dans l'org de B dont le
compte effectif est un compte de A ; un appel d'un compte de A dans l'org de B.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre import purge  # noqa: E402
from oto_mcp.export_perimetre.perimetre import ComptesHorsTenant, ComptesPartages  # noqa: E402
from perimetre_banc import A, B, membre, semer  # noqa: E402

C, D = "C5e2a", "D8b4f"
SUPPORT = "support-admin-banc"


def _appel(c, sub, org, effectif=None, args=None):
    c.execute("INSERT INTO tool_calls (server, kind, sub, effective_sub, tool, org_id, args) "
              "VALUES ('oto', 'tool', %s, %s, 'oto_doc', %s, %s)",
              (sub, effectif, org, json.dumps(args or {})))


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        semes = {m: semer(c, m) for m in (A, B, C, D)}
        a, b = semes[A], semes[B]
        # C partage un projet à un compte du tenant B : un partage vers un tiers.
        c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                  "principal_id, granted_by) VALUES ('project', %s, 'user', %s, %s)",
                  (str(semes[C]["projet"]), b["alice"], semes[C]["alice"]))
        # D compte dans son org un membre qui n'est pas un compte du tenant.
        membre(c, semes[D]["org"], "compte-hors-tenant")
        # Cas croisés entre A (purgé) et B (voisin).
        _appel(c, SUPPORT, a["org"], args={"vu": "support dans A"})      # part avec A
        _appel(c, SUPPORT, b["org"], args={"_m": B})                      # reste
        _appel(c, b["alice"], b["org"], effectif=a["alice"], args={"_m": B})   # reste
        _appel(c, a["alice"], b["org"], args={"_m": B})                   # reste (org de B)
        # Un ancien membre de A, sans appartenance : il est du périmètre.
        c.execute("INSERT INTO users (sub, email) VALUES (%s, %s)",
                  (f"{a['slug']}:{A}-ancien", f"ancien-{A}@exemple.test"))
        yield {"dsn": pg_module_dsn, **semes}


def _connexion(base):
    return psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row)


@contextmanager
def _lecture(base):
    with _connexion(base) as c, purge.en_lecture_seule(c):
        yield c


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


def test_a_blanc_ne_peut_pas_ecrire(base):
    with _lecture(base) as c, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        c.execute("DELETE FROM tool_calls WHERE FALSE")


def test_une_piece_conservee_qu_une_cascade_emporterait_refuse(base):
    with _lecture(base) as c, pytest.raises(purge.PurgeRefusee) as e:
        purge.a_blanc(c, base[B]["slug"])
    assert e.value.code == "reference_entrante"
    assert "billing_contracts" in str(e.value)


def test_un_partage_vers_un_tiers_refuse(base):
    with _connexion(base) as c:
        c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (base[C]["org"],))
    with _lecture(base) as c, pytest.raises(purge.PurgeRefusee) as e:
        purge.a_blanc(c, base[C]["slug"])
    assert e.value.code == "partage_vers_tiers"
    assert "resource_grants" in str(e.value)


@pytest.mark.parametrize("pret", ["compte", "equipe"])
def test_un_compte_de_messagerie_prete_hors_tenant_refuse(base, pret):
    a, b = base[A], base[B]
    with _connexion(base) as c:
        c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (a["org"],))
        if pret == "compte":
            sql = ("INSERT INTO connector_account_grants (owner_sub, provider, account_id, "
                   "grantee_sub, granted_by) VALUES (%s, 'LINKEDIN', 'x', %s, %s)")
            cible = b["alice"]
        else:
            sql = ("INSERT INTO connector_account_group_grants (owner_sub, provider, "
                   "account_id, grantee_group_id, granted_by) VALUES (%s, 'LINKEDIN', 'x', "
                   "%s, %s)")
            cible = c.execute("SELECT id FROM org_groups WHERE org_id = %s",
                              (b["org"],)).fetchone()["id"]
        c.execute(sql, (a["alice"], cible, a["alice"]))
    try:
        with _lecture(base) as c, pytest.raises(purge.PurgeRefusee) as e:
            purge.a_blanc(c, a["slug"])
        assert e.value.code == "partage_vers_tiers"
    finally:
        with _connexion(base) as c:
            c.execute("DELETE FROM connector_account_grants WHERE owner_sub = %s", (a["alice"],))
            c.execute("DELETE FROM connector_account_group_grants WHERE owner_sub = %s",
                      (a["alice"],))


def test_un_compte_du_tenant_membre_d_une_org_voisine_refuse(base):
    a, b = base[A], base[B]
    with _connexion(base) as c:
        membre(c, b["org"], a["bob"])
    try:
        with _lecture(base) as c, pytest.raises(ComptesPartages):
            purge.a_blanc(c, a["slug"])
    finally:
        with _connexion(base) as c:
            c.execute("DELETE FROM org_members WHERE org_id = %s AND sub = %s",
                      (b["org"], a["bob"]))


def test_un_ancien_compte_du_tenant_membre_ailleurs_refuse(base):
    a, b = base[A], base[B]
    ancien = f"{a['slug']}:{A}-ancien"
    with _connexion(base) as c:
        membre(c, b["org"], ancien)
    try:
        with _lecture(base) as c, pytest.raises(ComptesHorsTenant):
            purge.a_blanc(c, a["slug"])
    finally:
        with _connexion(base) as c:
            c.execute("DELETE FROM org_members WHERE org_id = %s AND sub = %s",
                      (b["org"], ancien))


def test_un_membre_hors_tenant_refuse(base):
    with _lecture(base) as c, pytest.raises(ComptesHorsTenant):
        purge.a_blanc(c, base[D]["slug"])


def test_le_tenant_primaire_ne_se_purge_pas(base):
    with _lecture(base) as c:
        slug = c.execute("SELECT slug FROM tenants WHERE id = 1").fetchone()["slug"]
        with pytest.raises(purge.PurgeRefusee) as e:
            purge.a_blanc(c, slug)
    assert e.value.code == "tenant_primaire"


def test_la_regle_du_journal_est_la_meme_en_sql_et_en_python(base):
    """Chaque appel de la base, classé par le SQL de la purge et par le classeur des
    archives : mêmes réponses (cas croisés compris)."""
    with _lecture(base) as c:
        lu = purge.lire(c, base[A]["slug"])
        colonnes, du_tenant = purge.classeur_du_journal(lu.perimetre)
        for r in c.execute(f"SELECT {', '.join(colonnes)}, ({lu.pred('tool_calls')}) AS sql "
                           "FROM tool_calls", lu.params):
            e = {k: "" if r[k] is None else str(r[k]) for k in colonnes}
            assert du_tenant(e) == bool(r["sql"]), r


def test_appliquer_vide_le_tenant_garde_le_voisin_puis_ne_fait_plus_rien(base):
    a = base[A]
    with _connexion(base) as c:
        c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (a["org"],))
        voisin, avant = _marques(c, B), _marques(c, A)
    conserve = {t: n for t, n in avant.items() if t in purge.CONSERVEES}
    with _lecture(base) as c:
        plan = purge.a_blanc(c, a["slug"])
        fige = purge.lire(c, a["slug"]).perimetre
    with _connexion(base) as c:
        assert _marques(c, A) == avant                     # à blanc : rien d'écrit
    assert plan.id_tenant == a["tenant"]
    assert len(plan.orgs) == 2                             # l'org et l'org d'inscription
    assert plan.comptes == 3                               # alice, bob, l'ancien membre
    assert f"{a['slug']}:{A}-ancien" in fige.subs
    for t in ("users", "orgs", "tenants", "projects", "docs", "datastore_rows", "nodes",
              "blocks", "tool_calls", "resource_grants", "connector_availability"):
        assert plan.supprimer[t] >= 1, t
    assert plan.supprimer["users"] == 3 and plan.supprimer["tenants"] == 1
    assert plan.appels_de_support == 1
    assert set(plan.conserver) == set(purge.CONSERVEES)
    with _lecture(base) as c:
        assert sum(purge.journal_par_mois(c, a["slug"]).values()) == plan.supprimer["tool_calls"]

    # Un refus de la base laisse TOUT intact, journal compris.
    with _connexion(base) as c:
        c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                  "principal_id, granted_by) VALUES ('project', %s, 'user', %s, %s)",
                  (str(a["projet"]), base[B]["bob"], a["alice"]))
        with pytest.raises(purge.PurgeRefusee):
            purge.appliquer(c, fige)
        assert _marques(c, A)["tool_calls"] == avant["tool_calls"]
        c.execute("DELETE FROM resource_grants WHERE principal_id = %s", (base[B]["bob"],))

    # La base (tout sauf le journal), puis le journal par tranches de 2 ids.
    with _connexion(base) as c:
        fait = purge.appliquer(c, fige)
        assert "tool_calls" not in fait.supprimer
        assert fait.supprimer == {t: n for t, n in plan.supprimer.items() if t != "tool_calls"}
        assert _compte_journal(c, fige) == plan.supprimer["tool_calls"]   # pas encore parti
        assert purge.vider_journal(c, fige, lot=2) == plan.supprimer["tool_calls"]
    with _lecture(base) as c:
        recompte = purge.recompter(c, fige)
    assert not any(recompte.supprimer.values()), recompte.supprimer
    with _connexion(base) as c:
        # Rien du tenant ne reste, hors nos pièces et les appels du voisin qui le citent ;
        # le voisin est intact, ligne pour ligne.
        assert _marques(c, A) == {**conserve, "tool_calls": 2}
        assert _marques(c, B) == voisin
        assert c.execute("SELECT count(*) AS n FROM tool_calls WHERE sub = %s",
                         (SUPPORT,)).fetchone()["n"] == 1             # celui dans B
    # Idempotent : rejoué, le geste ne trouve plus rien.
    with _lecture(base) as c:
        assert purge.a_blanc(c, a["slug"]).id_tenant is None
    with _connexion(base) as c:
        assert purge.appliquer(c, fige).vide
        assert purge.vider_journal(c, fige) == 0


def _compte_journal(c, fige) -> int:
    lu = purge.lecture(c, fige)
    return c.execute(f"SELECT count(*) AS n FROM tool_calls WHERE {lu.pred('tool_calls')}",
                     lu.params).fetchone()["n"]


def test_les_prefixes_d_objet_sont_ceux_des_proprietaires(base):
    with _lecture(base) as c:
        lu = purge.lire(c, base[C]["slug"])
        prefixes, hors = purge.objets(c, lu, "https://stockage-source.exemple.test")
    alice = base[C]["alice"].replace(":", "%3A")
    assert f"avatars/{alice}/" in prefixes and f"images/{alice}/" in prefixes
    assert f"org-logos/{base[C]['org']}/" in prefixes
    assert f"project-files/{base[C]['projet']}/" in prefixes
    assert all(p.endswith("/") for p in prefixes)
    # La clé du banc qui ne suit pas `<famille>/<projet>/` est rendue à part.
    assert hors == {f"project-files/{C}/abc/f.txt"}

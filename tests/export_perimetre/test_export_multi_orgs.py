"""L'export d'un périmètre, sur une vraie base à plusieurs orgs (oto-backend#1088).

Chaque ligne semée porte le MARQUEUR de son propriétaire dans une valeur texte
(`A7d1e` pour le périmètre exporté, `B9f3c` pour l'org voisine). Deux propriétés se
vérifient alors sans passer par les règles du classement — un second chemin, pas la
relecture de ce qu'on a écrit :
- **rien d'autrui** : aucune ligne du fichier ne contient le marqueur de la voisine ;
- **rien d'oublié** : pour chaque table, les lignes de la base qui portent le marqueur
  du périmètre sont exactement celles du fichier qui le portent.

Les refus (compte partagé, secret chiffré, référence hors périmètre) se prouvent chacun
sur une org à part, pour ne pas contaminer le périmètre principal.
"""
from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.extraction import (  # noqa: E402
    ReferencesHorsPerimetre, SecretsChiffres, exporter)
from oto_mcp.export_perimetre.perimetre import ComptesPartages, PerimetreRefuse  # noqa: E402

A, B = "A7d1e", "B9f3c"


def _org(c, nom: str, personal_of: str | None = None) -> int:
    return c.execute("INSERT INTO orgs (name, personal_of) VALUES (%s, %s) RETURNING id",
                     (nom, personal_of)).fetchone()["id"]


def _membre(c, org: int, sub: str) -> None:
    c.execute("INSERT INTO users (sub, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
              (sub, f"{sub}@exemple.test"))
    c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'admin')",
              (org, sub))


def _semer(c, m: str) -> dict:
    """Une org, deux comptes, un espace perso, une équipe et un contenu de chaque famille."""
    org = _org(c, f"org {m}")
    alice, bob = f"{m}-alice", f"{m}-bob"
    _membre(c, org, alice)
    _membre(c, org, bob)
    _membre(c, _org(c, f"perso {m}", personal_of=alice), alice)
    groupe = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
                       (org, f"équipe {m}")).fetchone()["id"]
    c.execute("INSERT INTO org_group_members (group_id, sub, group_role) "
              "VALUES (%s, %s, 'member')", (groupe, bob))
    projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                       "VALUES ('org', %s, %s) RETURNING id", (str(org), f"projet {m}")
                       ).fetchone()["id"]
    c.execute("INSERT INTO projects (owner_type, owner_id, name) VALUES ('user', %s, %s)",
              (alice, f"projet perso {m}"))
    page = c.execute("INSERT INTO docs (project_id, title, body_md) VALUES (%s, %s, %s) "
                     "RETURNING id", (projet, f"page {m}", f"corps {m}")).fetchone()["id"]
    c.execute("INSERT INTO doc_revisions (doc_id, title, body_md) VALUES (%s, %s, %s)",
              (page, f"page {m}", f"v1 {m}"))
    c.execute("INSERT INTO project_files (project_id, s3_key, filename, mime, size_bytes) "
              "VALUES (%s, %s, %s, 'text/plain', 3)", (projet, f"projets/{m}/f.txt", f"f {m}"))
    c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
              "principal_id, granted_by) VALUES ('project', %s, 'group', %s, %s)",
              (str(projet), str(groupe), alice))
    tableau = c.execute("INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                        "VALUES ('org', %s, %s) RETURNING id", (str(org), f"tableau_{m}")
                        ).fetchone()["id"]
    for i in (1, 2):
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                  (tableau, f"{m}-{i}", json.dumps({"nom": f"ligne {m}"})))
    noeud = c.execute("INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
                      "VALUES (%s, 'page', 'org', %s, %s) RETURNING id",
                      (f"n-{m}", str(org), json.dumps({"titre": m}))).fetchone()["id"]
    c.execute("INSERT INTO blocks (public_id, node_id, position, type, props) "
              "VALUES (%s, %s, 1, 'paragraph', %s)", (f"b-{m}", noeud, json.dumps({"t": m})))
    c.execute("INSERT INTO org_instructions (org_id, owner_type, owner_id, slug, body_md) "
              "VALUES (%s, 'org', %s, %s, %s)", (org, str(org), f"proc-{m}", f"fais {m}"))
    c.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES (%s, %s, %s, %s)",
              (f"run-{m}", alice, org, f"run {m}"))
    c.execute("INSERT INTO run_messages (run_id, seq, role, content) "
              "VALUES (%s, 1, 'user', %s)", (f"run-{m}", json.dumps({"texte": m})))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id) "
              "VALUES ('oto', 'tool', %s, 'oto_doc', %s)", (alice, org))
    c.execute("INSERT INTO usage (sub, tool, day, count) VALUES (%s, 'oto_doc', "
              "CURRENT_DATE, 3)", (bob,))
    c.execute("INSERT INTO billing_contracts (org_id, seats, unit_amount, reference, "
              "starts_at) VALUES (%s, 1, 100, %s, NOW())", (org, f"contrat {m}"))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool) "
              "VALUES ('oto', 'tool', %s, 'oto_whoami')", (bob,))
    c.execute("INSERT INTO org_entitlements (sub, right_key, value, source) "
              "VALUES (%s, 'essai', 1, %s)", (alice, f"commerce {m}"))
    return {"org": org, "projet": projet, "page": page}


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        yield {"dsn": pg_module_dsn, A: _semer(c, A), B: _semer(c, B)}


def _exporter(base, orgs, chemin):
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        return exporter(c, orgs, chemin)


def _lignes(chemin) -> list[dict]:
    return [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(scope="module")
def export_a(base, tmp_path_factory):
    chemin = tmp_path_factory.mktemp("export") / "a.jsonl"
    manifeste = _exporter(base, [base[A]["org"]], chemin)
    return chemin, manifeste


def test_aucune_ligne_d_un_autre_proprietaire(export_a):
    chemin, _ = export_a
    texte = chemin.read_text(encoding="utf-8")
    assert A in texte
    assert B not in texte


def test_toutes_les_lignes_marquees_du_perimetre_sont_parties(base, export_a):
    chemin, _ = export_a
    lignes = _lignes(chemin)[:-1]
    parties: dict[str, int] = {}
    for x in lignes:
        if A in json.dumps(x["l"], ensure_ascii=False):
            parties[x["t"]] = parties.get(x["t"], 0) + 1
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        attendues = {}
        for t, e in CLASSEMENT.items():
            if e.classe not in EXPORTEES:
                continue
            n = c.execute(f"SELECT count(*) AS n FROM {t} x "
                          "WHERE row_to_json(x)::text LIKE %s", (f"%{A}%",)).fetchone()["n"]
            if n:
                attendues[t] = n
    assert len(attendues) >= 15, attendues   # le banc exerce vraiment les familles
    assert parties == attendues


def test_le_manifeste_dit_ce_qui_ne_part_pas_et_ce_qui_vit_hors_base(base, export_a):
    chemin, manifeste = export_a
    assert _lignes(chemin)[-1] == {"manifeste": manifeste}
    assert manifeste["tables"]["billing_contracts"] == {
        "classe": "exclue", "raison": CLASSEMENT["billing_contracts"].raison, "omises": 1}
    # Une ligne SANS org (droit « personne, partout », appel hors org) est celle de son
    # compte : elle se compte, ou elle part — elle ne tombe pas hors périmètre.
    assert manifeste["tables"]["org_entitlements"]["omises"] == 1
    assert manifeste["tables"]["tool_calls"]["lignes"] == 2
    assert manifeste["hors_base"] == {"project_files.s3_key": [f"projets/{A}/f.txt"]}
    assert manifeste["references_instance"] == {"orgs(tenant_id) → tenants": ["1"]}
    assert manifeste["perimetre"]["orgs_declarees"] == [base[A]["org"]]
    assert len(manifeste["perimetre"]["orgs_personnelles"]) == 1
    assert manifeste["perimetre"]["comptes"] == 2
    assert manifeste["sequences"]["orgs.id"] >= base[A]["org"]
    compte = {t: v["lignes"] for t, v in manifeste["tables"].items() if "lignes" in v}
    assert sum(compte.values()) == len(_lignes(chemin)) - 1


def test_les_parents_precedent_leurs_enfants_dans_le_fichier(export_a):
    chemin, manifeste = export_a
    vus = [x["t"] for x in _lignes(chemin)[:-1]]
    premiers = {t: vus.index(t) for t in set(vus)}
    assert premiers["projects"] < premiers["docs"] < premiers["doc_revisions"]
    assert premiers["user_datastores"] < premiers["datastore_rows"]
    assert premiers["runs"] < premiers["run_messages"]


def test_un_export_ne_s_ecrase_pas(base, export_a):
    chemin, _ = export_a
    with pytest.raises(FileExistsError):
        _exporter(base, [base[A]["org"]], chemin)


def test_une_org_inconnue_est_refusee(base, tmp_path):
    with pytest.raises(PerimetreRefuse, match="introuvable"):
        _exporter(base, [987654321], tmp_path / "x.jsonl")
    assert not (tmp_path / "x.jsonl").exists()


def test_un_compte_partage_avec_une_org_hors_perimetre_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        org = _org(c, "org partagée")
        _membre(c, org, "partage-dave")
        c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'member')",
                  (base[B]["org"], "partage-dave"))
    with pytest.raises(ComptesPartages, match="partage-dave") as e:
        _exporter(base, [org], tmp_path / "x.jsonl")
    assert e.value.partages == {"partage-dave": [base[B]["org"]]}


def test_un_secret_chiffre_du_perimetre_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        org = _org(c, "org à secret")
        c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                  "account, secret_enc) VALUES ('org', %s, 'serper', '', 'chiffre')",
                  (str(org),))
    with pytest.raises(SecretsChiffres) as e:
        _exporter(base, [org], tmp_path / "x.jsonl")
    assert e.value.comptes == {"connector_credentials": 1}
    assert not (tmp_path / "x.jsonl").exists()


def test_une_reference_vers_une_ligne_d_autrui_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        org = _org(c, "org qui pointe ailleurs")
        projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                           "VALUES ('org', %s, 'p') RETURNING id", (str(org),)
                           ).fetchone()["id"]
        page = c.execute("INSERT INTO docs (project_id, title, body_md) "
                         "VALUES (%s, 't', '') RETURNING id", (projet,)).fetchone()["id"]
        c.execute("INSERT INTO doc_links (from_doc, to_doc) VALUES (%s, %s)",
                  (page, base[B]["page"]))
    with pytest.raises(ReferencesHorsPerimetre) as e:
        _exporter(base, [org], tmp_path / "x.jsonl")
    assert e.value.comptes == {"doc_links(to_doc) → docs": 1}


def test_l_export_se_fait_en_lecture_seule(base, tmp_path):
    """La transaction d'export refuse toute écriture : c'est la BASE qui le garantit."""
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        exporter(c, [base[A]["org"]], tmp_path / "a.jsonl")
        assert c.read_only is True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO usage (sub, tool, day, count) "
                      "VALUES ('x', 'y', CURRENT_DATE, 1)")
    texte = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert A in texte and B not in texte

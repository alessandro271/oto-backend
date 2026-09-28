"""L'export d'un périmètre, sur une vraie base à plusieurs tenants et orgs (oto-backend#1088).

Chaque ligne semée porte le MARQUEUR de son propriétaire dans une valeur texte
(`A7d1e` pour le périmètre exporté, `B9f3c` pour le voisin ; `perimetre_banc`). Deux
propriétés se vérifient alors sans passer par les règles du classement — un second
chemin, pas la relecture de ce qu'on a écrit :
- **rien d'autrui** : aucune ligne du fichier ne contient le marqueur du voisin ;
- **rien d'oublié** : pour chaque table, les lignes de la base qui portent le marqueur
  du périmètre sont exactement celles du fichier qui le portent.

Les refus se prouvent chacun sur un tenant à part, pour ne pas contaminer le
périmètre principal.
"""
from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.extraction import (  # noqa: E402
    ReferencesHorsPerimetre, SecretsChiffres, exporter)
from oto_mcp.export_perimetre.perimetre import (  # noqa: E402
    ComptesHorsTenant, ComptesPartages, PerimetreRefuse, TenantPartage, TenantsMultiples)
from perimetre_banc import A, B, membre, org, semer, tenant  # noqa: E402


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        yield {"dsn": pg_module_dsn, A: semer(c, A), B: semer(c, B)}


def _exporter(base, orgs, chemin, **kw):
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        return exporter(c, orgs, chemin, **kw)


def _lignes(chemin) -> list[dict]:
    return [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines()]


def _tenant_a_part(c, slug: str) -> int:
    return tenant(c, slug, slug)


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
    parties: dict[str, int] = {}
    for x in _lignes(chemin)[:-1]:
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
    assert len(attendues) >= 20, attendues   # le banc exerce vraiment les familles
    assert parties == attendues


def test_le_manifeste_dit_ce_qui_ne_part_pas_et_ce_qui_vit_hors_base(base, export_a):
    chemin, manifeste = export_a
    a = base[A]
    assert _lignes(chemin)[-1] == {"manifeste": manifeste}
    assert manifeste["tables"]["billing_contracts"] == {
        "classe": "exclue", "raison": CLASSEMENT["billing_contracts"].raison, "omises": 1}
    # Une ligne SANS org (droit « personne, partout », appel hors org) est celle de son
    # compte : elle se compte, ou elle part — elle ne tombe pas hors périmètre.
    assert manifeste["tables"]["org_entitlements"]["omises"] == 1
    assert manifeste["tables"]["tool_calls"]["lignes"] == 2
    assert manifeste["hors_base"] == {"project_files.s3_key": [f"projets/{A}/f.txt"]}
    # Le tenant PART : plus aucune référence vers l'instance, et l'import sait quoi remapper.
    assert manifeste["references_instance"] == {}
    assert manifeste["tables"]["tenants"]["lignes"] == 1
    assert manifeste["tenant"] == {"id": a["tenant"], "slug": a["slug"],
                                   "primaire_source": False}
    assert manifeste["comptes"] == {a["alice"]: f"{A}-alice", a["bob"]: f"{A}-bob"}
    assert manifeste["secrets"] == {}
    assert manifeste["perimetre"]["orgs_declarees"] == [a["org"]]
    assert len(manifeste["perimetre"]["orgs_personnelles"]) == 1
    assert manifeste["perimetre"]["comptes"] == 2
    assert manifeste["sequences"]["orgs.id"] >= a["org"]
    compte = {t: v["lignes"] for t, v in manifeste["tables"].items() if "lignes" in v}
    assert sum(compte.values()) == len(_lignes(chemin)) - 1


def test_les_parents_precedent_leurs_enfants_dans_le_fichier(export_a):
    chemin, manifeste = export_a
    vus = [x["t"] for x in _lignes(chemin)[:-1]]
    premiers = {t: vus.index(t) for t in set(vus)}
    assert premiers["projects"] < premiers["docs"] < premiers["doc_revisions"]
    assert premiers["user_datastores"] < premiers["datastore_rows"]
    assert premiers["runs"] < premiers["run_messages"]
    assert premiers["tenants"] < premiers["tenant_admins"]


def test_un_export_ne_s_ecrase_pas(base, export_a):
    chemin, _ = export_a
    with pytest.raises(FileExistsError):
        _exporter(base, [base[A]["org"]], chemin)


def test_une_org_inconnue_est_refusee(base, tmp_path):
    with pytest.raises(PerimetreRefuse, match="introuvable"):
        _exporter(base, [987654321], tmp_path / "x.jsonl")
    assert not (tmp_path / "x.jsonl").exists()


def test_deux_tenants_dans_un_perimetre_refusent(base, tmp_path):
    # Avant le test du compte partagé : celui-ci rattache un compte tiers à B.
    with pytest.raises(TenantsMultiples):
        _exporter(base, [base[A]["org"], base[B]["org"]], tmp_path / "x.jsonl")


def test_un_compte_partage_avec_une_org_hors_perimetre_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org partagée", _tenant_a_part(c, "tpartage"))
        membre(c, o, "tpartage:dave")
        c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'member')",
                  (base[B]["org"], "tpartage:dave"))
    with pytest.raises(ComptesPartages, match="dave") as e:
        _exporter(base, [o], tmp_path / "x.jsonl")
    assert e.value.partages == {"tpartage:dave": [base[B]["org"]]}


def test_un_tenant_qui_heberge_une_org_hors_perimetre_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        tid = _tenant_a_part(c, "tdeux")
        seule, autre = org(c, "déclarée", tid), org(c, "oubliée", tid)
    with pytest.raises(TenantPartage, match=str(autre)):
        _exporter(base, [seule], tmp_path / "x.jsonl")


def test_un_compte_que_le_tenant_ne_qualifie_pas_refuse(base, tmp_path):
    """Sur la cible le tenant est primaire, ses subs y sont nus : un sub sans son
    préfixe est celui d'un autre annuaire."""
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org hors annuaire", _tenant_a_part(c, "thors"))
        membre(c, o, "compte-nu")
    with pytest.raises(ComptesHorsTenant, match="compte-nu"):
        _exporter(base, [o], tmp_path / "x.jsonl")


def test_un_secret_chiffre_refuse_sauf_a_le_transporter(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org à secret", _tenant_a_part(c, "tsecret"))
        c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                  "account, secret_enc) VALUES ('org', %s, 'serper', '', 'chiffre')",
                  (str(o),))
    with pytest.raises(SecretsChiffres) as e:
        _exporter(base, [o], tmp_path / "x.jsonl")
    assert e.value.comptes == {"connector_credentials": 1}
    assert not (tmp_path / "x.jsonl").exists()
    manifeste = _exporter(base, [o], tmp_path / "x.jsonl", transporter_secrets=True)
    assert manifeste["secrets"] == {"connector_credentials": 1}


def test_une_reference_vers_une_ligne_d_autrui_refuse(base, tmp_path):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org qui pointe ailleurs", _tenant_a_part(c, "tpointe"))
        projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                           "VALUES ('org', %s, 'p') RETURNING id", (str(o),)
                           ).fetchone()["id"]
        page = c.execute("INSERT INTO docs (project_id, title, body_md) "
                         "VALUES (%s, 't', '') RETURNING id", (projet,)).fetchone()["id"]
        c.execute("INSERT INTO doc_links (from_doc, to_doc) VALUES (%s, %s)",
                  (page, base[B]["page"]))
    with pytest.raises(ReferencesHorsPerimetre) as e:
        _exporter(base, [o], tmp_path / "x.jsonl")
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

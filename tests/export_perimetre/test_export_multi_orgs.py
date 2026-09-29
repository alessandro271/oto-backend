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
import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import credentials_store  # noqa: E402
from oto_mcp.crypto import decrypt_with_key, encrypt_with_key  # noqa: E402
from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.extraction import (  # noqa: E402
    ReferencesHorsPerimetre, SecretsChiffres, exporter)
from oto_mcp.export_perimetre.perimetre import (  # noqa: E402
    ComptesHorsTenant, ComptesPartages, PerimetreRefuse, TenantPartage, TenantsMultiples)
from oto_mcp.export_perimetre.rechiffrement import empreinte_cle  # noqa: E402
from oto_mcp.export_perimetre.objets import StockageS3  # noqa: E402
from perimetre_banc import A, B, BASE_SOURCE, FauxS3, membre, org, semer, tenant  # noqa: E402

CLE_CIBLE = os.urandom(32)


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        a, b = semer(c, A), semer(c, B)
        yield {"dsn": pg_module_dsn, A: a, B: b,
               "stockage": StockageS3(FauxS3({**a["objets"], **b["objets"]}), "source")}


def _exporter(base, orgs, chemin, **kw):
    """Comme la commande : notre stockage (faux), notre base publique, et la clé de la
    cible — l'archive des objets se scelle sous elle."""
    kw.setdefault("base_publique", BASE_SOURCE)
    kw.setdefault("stockage", base["stockage"])
    kw.setdefault("cle_cible", CLE_CIBLE)
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
    # Les objets du périmètre, par leur clé ou par leur URL, où qu'elle soit (avatar,
    # logo, fichier public, image collée dans une page ou une ligne de tableau).
    objets = manifeste["objets"]
    assert set(objets["liste"]) == set(a["objets"])
    assert objets["base_publique"] == BASE_SOURCE
    assert (chemin.parent / objets["archive"]).is_file()
    # Le tenant PART : plus aucune référence vers l'instance, et l'import sait quoi remapper.
    assert manifeste["references_instance"] == {}
    assert manifeste["tables"]["tenants"]["lignes"] == 1
    assert manifeste["tenant"] == {"id": a["tenant"], "slug": a["slug"],
                                   "nom": f"tenant {A}", "primaire_source": False}
    assert manifeste["comptes"] == {a["alice"]: f"{A}-alice", a["bob"]: f"{A}-bob"}
    assert manifeste["secrets"] == {} and manifeste["cle_cible"] == empreinte_cle(CLE_CIBLE)
    assert manifeste["partages_omis"] == {}
    assert manifeste["tenant"]["nom"] == f"tenant {A}"
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


def test_un_secret_refuse_sans_la_cle_cible_et_part_rechiffre_avec(base, tmp_path,
                                                                  monkeypatch):
    """Le fichier ne porte JAMAIS un secret sous notre clé : sans la clé de la cible,
    l'export refuse ; avec, il rechiffre, et le manifeste dit sous quelle clé."""
    cle_source, cle_cible = os.urandom(32), os.urandom(32)
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", cle_source.hex())
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org à secret", _tenant_a_part(c, "tsecret"))
        aad = credentials_store._aad("org", str(o), "serper")
        source = encrypt_with_key(cle_source, "clair", aad)
        c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                  "account, secret_enc) VALUES ('org', %s, 'serper', '', %s)", (str(o), source))
    with pytest.raises(SecretsChiffres) as e:
        _exporter(base, [o], tmp_path / "x.jsonl", cle_cible=None)
    assert e.value.comptes == {"connector_credentials": 1}
    assert not (tmp_path / "x.jsonl").exists()
    manifeste = _exporter(base, [o], tmp_path / "x.jsonl", cle_cible=cle_cible)
    assert manifeste["secrets"] == {"connector_credentials": 1}
    assert manifeste["cle_cible"] == empreinte_cle(cle_cible)
    ligne = next(x["l"] for x in _lignes(tmp_path / "x.jsonl")[:-1]
                 if x["t"] == "connector_credentials")
    assert ligne["secret_enc"] != source
    assert decrypt_with_key(cle_cible, ligne["secret_enc"], aad) == "clair"
    texte = (tmp_path / "x.jsonl").read_text(encoding="utf-8")
    assert "clair" not in texte and cle_cible.hex() not in texte


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


def test_un_partage_vers_un_destinataire_hors_perimetre_est_omis_et_compte(base, tmp_path):
    """Décision du 28/09/2026 : il ne part pas — sur la cible ce destinataire n'existe
    pas — et le manifeste le compte."""
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org qui partage dehors", _tenant_a_part(c, "tdehors"))
        projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                           "VALUES ('org', %s, 'p') RETURNING id", (str(o),)
                           ).fetchone()["id"]
        c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                  "principal_id) VALUES ('project', %s, 'org', %s)",
                  (str(projet), str(base[B]["org"])))
        c.execute("INSERT INTO grants (resource_id, grantor_kind, grantor_id, grantee_kind, "
                  "grantee_id) VALUES ('inst:1', 'org', %s, 'user', %s)",
                  (str(o), base[B]["alice"]))
    manifeste = _exporter(base, [o], tmp_path / "x.jsonl")
    assert manifeste["partages_omis"] == {"resource_grants": 1, "grants": 1}
    parties = {x["t"] for x in _lignes(tmp_path / "x.jsonl")[:-1]}
    assert not parties & {"resource_grants", "grants"}


def test_l_export_se_fait_en_lecture_seule(base, tmp_path):
    """La transaction d'export refuse toute écriture : c'est la BASE qui le garantit."""
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        exporter(c, [base[A]["org"]], tmp_path / "a.jsonl", base_publique=BASE_SOURCE,
                 stockage=base["stockage"], cle_cible=CLE_CIBLE)
        assert c.read_only is True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO usage (sub, tool, day, count) "
                      "VALUES ('x', 'y', CURRENT_DATE, 1)")
    texte = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert A in texte and B not in texte

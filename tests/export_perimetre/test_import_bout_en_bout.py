"""Bout en bout : exporter un propriétaire, faire naître sa base, l'y importer (#1088).

Deux vraies bases de test locales : une SOURCE à plusieurs tenants et orgs (`live`,
tenant primaire `oto`), et une CIBLE née par le démarrage normal (`init_db`) pour
une instance qui déclare le tenant du propriétaire comme le sien
(`OTO_TENANT_PRIMAIRE_SLUG`, `OTO_BRAND_NAME`). Chaque instance a SA clé maîtresse :
l'export tourne sous la clé source et reçoit la clé cible, l'import ne connaît que la
clé cible (décision du 28/09/2026).

Ce qui est vérifié sur la cible, par des chemins indépendants de l'outil :
- **rien d'autrui** : aucune ligne ne porte le marqueur du voisin ;
- **rien d'oublié** : par table, autant de lignes marquées que dans la source ;
- **le tenant est la ligne 1**, toutes les orgs y sont rattachées, et les comptes y
  sont NUS (le préfixe du tenant tiers est tombé, jusque dans les JSON) ;
- **les secrets se lisent avec la clé CIBLE**, sous l'AAD de leur ligne cible, et plus
  avec la clé source — qui n'a jamais quitté l'export.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import credentials_store, runner_hook, transcription_worker  # noqa: E402
from oto_mcp.crypto import decrypt_with_key, encrypt_with_key  # noqa: E402
from oto_mcp.export_perimetre import commande  # noqa: E402
from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.decouverte import Cle, Schema  # noqa: E402
from oto_mcp.export_perimetre.extraction import exporter  # noqa: E402
from oto_mcp.export_perimetre.importation import (  # noqa: E402
    TAILLE_LOT, ImportRefuse, importer)
from oto_mcp.export_perimetre.rechiffrement import AAD, empreinte_cle  # noqa: E402
from oto_mcp.export_perimetre.transformation import Transformation  # noqa: E402
from perimetre_banc import A, B, SECRET, semer, slug_de  # noqa: E402

CLE_SOURCE, CLE_CIBLE = os.urandom(32), os.urandom(32)
NOM_A = f"tenant {A}"


def _naitre(pg_dsn: str, slug: str, nom: str = NOM_A) -> str:
    """Une base NEUVE montée par le démarrage normal, pour l'instance du tenant `slug`."""
    from oto_mcp.db import _conn, init_db
    base = "oto_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(pg_dsn, autocommit=True) as root:
        root.execute(f'CREATE DATABASE "{base}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + base
    pool_avant = _conn._pool
    _conn._pool = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", dsn)
        mp.setenv("OTO_TENANT_PRIMAIRE_SLUG", slug)
        mp.setenv("OTO_BRAND_NAME", nom)
        try:
            init_db()
        finally:
            if _conn._pool is not None:
                _conn._pool.close()
            _conn._pool = pool_avant
    return dsn


def _detruire(pg_dsn: str, dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as root:
        root.execute(f'DROP DATABASE IF EXISTS "{dsn.rsplit("/", 1)[1]}" WITH (FORCE)')


def _importer(dsn: str, chemin, cle: bytes | None = CLE_CIBLE) -> dict:
    """L'import, tel que l'instance CIBLE le joue : sous SA clé, et seulement la sienne."""
    with pytest.MonkeyPatch.context() as mp:
        if cle is None:
            mp.delenv("OTO_MCP_MASTER_KEY", raising=False)
        else:
            mp.setenv("OTO_MCP_MASTER_KEY", cle.hex())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            return importer(c, chemin)


def _marquees(dsn: str, marqueur: str) -> dict[str, int]:
    out = {}
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        for t, e in CLASSEMENT.items():
            if e.classe not in EXPORTEES:
                continue
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{marqueur}%",)).fetchone()["n"]
            if n:
                out[t] = n
    return out


@pytest.fixture(scope="module")
def source(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        a, b = semer(c, A, cle=CLE_SOURCE), semer(c, B, cle=CLE_SOURCE)
        # Un journal qui dépasse plusieurs lots d'écriture (`importation.TAILLE_LOT`).
        c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id, args) "
                  "SELECT 'oto', 'tool', %s, 'oto_doc', %s, "
                  "jsonb_build_object('_m', %s::text, 'n', g) FROM generate_series(1, 1234) g",
                  (a["alice"], a["org"], A))
        yield {"dsn": pg_module_dsn, A: a, B: b}


@pytest.fixture(scope="module")
def export_a(source, tmp_path_factory):
    chemin = tmp_path_factory.mktemp("bout") / "a.jsonl"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
        with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
            manifeste = exporter(c, [source[A]["org"]], chemin, cle_cible=CLE_CIBLE)
    return chemin, manifeste


@pytest.fixture(scope="module")
def cible(source, export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        yield {"dsn": dsn, "rapport": _importer(dsn, export_a[0])}
    finally:
        _detruire(pg_dsn, dsn)


def test_le_fichier_ne_porte_que_des_secrets_pour_la_cible(export_a):
    chemin, manifeste = export_a
    assert manifeste["secrets"] == {"connector_credentials": 4, "runner_triggers": 1,
                                    "transcription_jobs": 1}
    assert manifeste["cle_cible"] == empreinte_cle(CLE_CIBLE)
    texte = chemin.read_text(encoding="utf-8")
    assert "clair-" not in texte
    assert CLE_CIBLE.hex() not in texte and CLE_SOURCE.hex() not in texte
    transformation = Transformation.depuis(_schema_vide_de_tenant(), manifeste["comptes"])
    for x in map(json.loads, texte.splitlines()[:-1]):
        if x["t"] in AAD:
            colonne, aad = AAD[x["t"]]
            ligne = transformation.appliquer(x["t"], x["l"])
            decrypt_with_key(CLE_CIBLE, ligne[colonne], aad(ligne))
            with pytest.raises(RuntimeError):
                decrypt_with_key(CLE_SOURCE, ligne[colonne], aad(x["l"]))


def _schema_vide_de_tenant() -> Schema:
    return Schema({}, {}, {}, (), {}, {})


def test_rien_d_autrui_et_rien_d_oublie_sur_la_cible(source, cible):
    assert _marquees(cible["dsn"], B) == {}
    assert _marquees(cible["dsn"], A) == _marquees(source["dsn"], A)


def test_le_rapport_d_import_suit_le_manifeste(cible, export_a):
    _, manifeste = export_a
    attendus = {t: v["lignes"] for t, v in manifeste["tables"].items() if v.get("lignes")}
    assert {t: v["lignes"] for t, v in cible["rapport"].items()} == attendus
    assert attendus["tool_calls"] > 2 * TAILLE_LOT   # plusieurs lots d'écriture


def test_le_tenant_du_proprietaire_est_la_ligne_1(cible):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        tenants = c.execute("SELECT id, slug, name FROM tenants").fetchall()
        assert tenants == [{"id": 1, "slug": slug_de(A), "name": NOM_A}]
        assert {r["tenant_id"] for r in c.execute("SELECT tenant_id FROM orgs")} == {1}
        assert c.execute("SELECT slug FROM tenant_admins").fetchall() == [{"slug": slug_de(A)}]


def test_les_comptes_sont_nus_sur_la_cible(cible):
    prefixe = f"{slug_de(A)}:"
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        subs = sorted(r["sub"] for r in c.execute("SELECT sub FROM users"))
        assert subs == [f"{A}-alice", f"{A}-bob"]
        for t in CLASSEMENT:
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{prefixe}%",)).fetchone()["n"]
            assert n == 0, f"{t} porte encore un sub qualifié"
        args = c.execute("SELECT args FROM tool_calls WHERE org_id IS NOT NULL").fetchone()
        assert args["args"]["pour"] == f"{A}-alice"


def test_les_secrets_se_lisent_avec_la_cle_cible(cible):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        lus = {}
        for t, (colonne, aad) in AAD.items():
            for ligne in c.execute(f"SELECT * FROM {t} WHERE {colonne} IS NOT NULL"):
                lus[(t, ligne.get("connector"))] = decrypt_with_key(
                    CLE_CIBLE, ligne[colonne], aad(ligne))
                with pytest.raises(RuntimeError, match="indéchiffrable"):
                    decrypt_with_key(CLE_SOURCE, ligne[colonne], aad(ligne))
        membre = c.execute("SELECT entity_id FROM connector_credentials "
                           "WHERE entity_type = 'member'").fetchone()["entity_id"]
    assert membre.endswith(f":{A}-alice")
    assert lus == {
        ("connector_credentials", "serper"): SECRET.format(A, "serper"),
        ("connector_credentials", "apollo"): SECRET.format(A, "apollo"),
        ("connector_credentials", "tavily"): SECRET.format(A, "tavily"),
        ("connector_credentials", "pappers"): SECRET.format(A, "pappers"),
        ("runner_triggers", None): SECRET.format(A, "hook"),
        ("transcription_jobs", None): SECRET.format(A, "transcription"),
    }


def test_les_aad_viennent_du_code_qui_ecrit_les_secrets():
    assert set(AAD) == {t for t, e in CLASSEMENT.items() if e.secrets}
    assert all(AAD[t][0] in CLASSEMENT[t].secrets for t in AAD)
    assert AAD["connector_credentials"][1]({"entity_type": "user", "entity_id": "s",
                                             "connector": "c", "account": ""}) == \
        credentials_store._aad("user", "s", "c")
    assert AAD["runner_triggers"][1]({"id": 7}) == runner_hook._aad_du_secret(7)
    assert AAD["transcription_jobs"][1]({"audio_key": "k"}) == transcription_worker._aad("k")


def test_la_transformation_denude_et_rattache_au_tenant_primaire():
    schema = Schema({}, {}, {}, (Cle("orgs", ("tenant_id",), "tenants", ("id",)),), {}, {})
    t = Transformation.depuis(schema, {"t1:abc": "abc", "nu": "nu"})
    assert t.comptes == {"t1:abc": "abc"}
    assert t.appliquer("orgs", {"tenant_id": 7, "created_by": "t1:abc"}) == \
        {"tenant_id": 1, "created_by": "abc"}
    assert t.appliquer("x", {"e": "12:t1:abc", "j": {"l": ["t1:abc", "t1:abcd"]}}) == \
        {"e": "12:abc", "j": {"l": ["abc", "t1:abcd"]}}
    assert t.appliquer("tenants", {"id": 9})["id"] == 1


def test_les_sequences_sont_recalees(cible, export_a):
    _, manifeste = export_a
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        for cle, maximum in manifeste["sequences"].items():
            sequence = c.execute("SELECT pg_get_serial_sequence(%s, %s) AS s",
                                 tuple(cle.split("."))).fetchone()["s"]
            dernier = c.execute(f"SELECT last_value FROM {sequence}").fetchone()["last_value"]
            assert dernier >= maximum, cle


def test_une_cible_deja_peuplee_refuse(cible, export_a):
    with pytest.raises(ImportRefuse, match="pas vierge"):
        _importer(cible["dsn"], export_a[0])


def test_une_cible_d_un_autre_tenant_ou_d_un_autre_nom_refuse(export_a, pg_dsn):
    for slug, nom, motif in (("autre-instance", NOM_A, "tenant primaire"),
                             (slug_de(A), "autre marque", "'autre marque'.*'tenant A7d1e'")):
        dsn = _naitre(pg_dsn, slug, nom)
        try:
            with pytest.raises(ImportRefuse, match=motif):
                _importer(dsn, export_a[0])
            with psycopg.connect(dsn, row_factory=dict_row) as c:
                assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
        finally:
            _detruire(pg_dsn, dsn)


def test_une_instance_sans_la_cle_ou_avec_une_autre_refuse(export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="pas de clé maîtresse"):
            _importer(dsn, export_a[0], cle=None)
        with pytest.raises(ImportRefuse, match="autre clé"):
            _importer(dsn, export_a[0], cle=os.urandom(32))
    finally:
        _detruire(pg_dsn, dsn)


def test_un_fichier_modifie_refuse(export_a, tmp_path, pg_dsn):
    chemin, _ = export_a
    lignes = chemin.read_text(encoding="utf-8").splitlines(keepends=True)
    i = next(i for i, x in enumerate(lignes) if A in x)
    lignes[i] = lignes[i].replace(A, "Z0000")
    abime = tmp_path / "abime.jsonl"
    abime.write_text("".join(lignes), encoding="utf-8")
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="empreinte"):
            _importer(dsn, abime)
    finally:
        _detruire(pg_dsn, dsn)


def test_une_cible_qui_ne_relit_pas_ce_qui_a_ete_ecrit_annule_tout(export_a, pg_dsn,
                                                                    monkeypatch):
    """La vérification finale mord : un tenant primaire mal repris fait tout annuler."""
    from oto_mcp.export_perimetre import importation
    monkeypatch.setattr(importation, "_ecrire_tenant_primaire", lambda conn, ligne: None)
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(importation.VerificationEchouee, match="tenants"):
            _importer(dsn, export_a[0])
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)


def test_la_commande_exporte_puis_importe(source, pg_dsn, tmp_path, monkeypatch, capsys):
    """`oto-mcp perimetre export|import` : la clé cible par l'environnement, pour cette
    seule exécution ; un refus sort en code 2, nommé, sans rien écrire."""
    chemin = tmp_path / "cli.jsonl"
    monkeypatch.setenv("DATABASE_URL", source["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
    assert commande.main(["export", "--org", str(source[A]["org"]),
                          "--sortie", str(chemin)]) == 2
    assert "SecretsChiffres" in capsys.readouterr().err and not chemin.exists()
    monkeypatch.setenv("OTO_EXPORT_CLE_CIBLE", CLE_CIBLE.hex())
    assert commande.main(["export", "--org", str(source[A]["org"]),
                          "--sortie", str(chemin)]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["cle_cible"] == empreinte_cle(CLE_CIBLE)
    assert CLE_CIBLE.hex() not in json.dumps(resume)
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        monkeypatch.delenv("OTO_EXPORT_CLE_CIBLE")
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_CIBLE.hex())
        assert commande.main(["import", str(chemin)]) == 0
        assert json.loads(capsys.readouterr().out) == resume["lignes"]
    finally:
        _detruire(pg_dsn, dsn)


# --- Un seul chemin (décision du 28/09/2026) : le rechiffrement est À L'EXPORT --------


def test_l_import_n_a_aucun_chemin_de_rechiffrement():
    """L'ancien chemin (l'import recevait les deux clés et rechiffrait) est RETIRÉ, pas
    gardé en variante : l'import ne prend aucune clé, n'en chiffre aucune, et l'export
    n'a plus de mode qui transporterait un secret sous notre clé."""
    from oto_mcp.export_perimetre import importation
    assert list(inspect.signature(importer).parameters) == ["conn", "chemin"]
    assert set(inspect.signature(exporter).parameters) == {"conn", "orgs", "sortie",
                                                            "cle_cible", "classement"}
    source = inspect.getsource(importation)
    assert "encrypt_with_key" not in source and "rechiffrer" not in source


def test_l_import_refuse_notre_cle(export_a, pg_dsn):
    """Une instance qui se présenterait avec NOTRE clé (celle de la source) n'importe rien."""
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="autre clé"):
            _importer(dsn, export_a[0], cle=CLE_SOURCE)
    finally:
        _detruire(pg_dsn, dsn)


def test_un_secret_chiffre_sous_une_autre_cle_que_la_cible_refuse(export_a, tmp_path,
                                                                   pg_dsn):
    """Un fichier dont UN secret n'est pas chiffré pour la cible (ici : sous notre clé),
    même au manifeste recalculé pour passer le contrôle d'empreinte, ne s'importe pas."""
    chemin, manifeste = export_a
    lignes = [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines()[:-1]]
    transformation = Transformation.depuis(_schema_vide_de_tenant(), manifeste["comptes"])
    x = next(x for x in lignes if x["t"] == "connector_credentials")
    colonne, aad = AAD[x["t"]]
    x["l"][colonne] = encrypt_with_key(CLE_SOURCE, "forgé",
                                       aad(transformation.appliquer(x["t"], x["l"])))
    textes = [json.dumps(y, ensure_ascii=False) + "\n" for y in lignes]
    empreinte = hashlib.sha256("".join(textes).encode()).hexdigest()
    forge = tmp_path / "forge.jsonl"
    forge.write_text("".join(textes) + json.dumps(
        {"manifeste": {**manifeste, "empreinte": empreinte}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="ne se déchiffre pas"):
            _importer(dsn, forge)
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)

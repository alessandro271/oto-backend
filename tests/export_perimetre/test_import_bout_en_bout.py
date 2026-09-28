"""Bout en bout : exporter un propriétaire, faire naître sa base, l'y importer (#1088).

Deux vraies bases de test locales : une SOURCE à plusieurs tenants et orgs (`live`,
tenant primaire `oto`), et une CIBLE née par le démarrage normal (`init_db`) pour
une instance qui déclare le tenant du propriétaire comme le sien
(`OTO_TENANT_PRIMAIRE_SLUG`). Chaque base a sa clé maîtresse.

Ce qui est vérifié sur la cible, par des chemins indépendants de l'outil :
- **rien d'autrui** : aucune ligne ne porte le marqueur du voisin ;
- **rien d'oublié** : par table, autant de lignes marquées que dans la source ;
- **le tenant est la ligne 1**, toutes les orgs y sont rattachées, et les comptes y
  sont NUS (le préfixe du tenant tiers est tombé, jusque dans les JSON) ;
- **les secrets se lisent avec la clé CIBLE**, sous l'AAD de leur ligne cible, et plus
  avec la clé source.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import credentials_store, runner_hook, transcription_worker  # noqa: E402
from oto_mcp.crypto import decrypt_with_key  # noqa: E402
from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.extraction import exporter  # noqa: E402
from oto_mcp.export_perimetre.importation import ImportRefuse, importer  # noqa: E402
from oto_mcp.export_perimetre.rechiffrement import AAD, Cles  # noqa: E402
from perimetre_banc import A, B, SECRET, semer, slug_de  # noqa: E402

CLES = Cles(source=os.urandom(32), cible=os.urandom(32))


def _naitre(pg_dsn: str, slug: str) -> str:
    """Une base NEUVE montée par le démarrage normal, pour l'instance du tenant `slug`."""
    from oto_mcp.db import _conn, init_db
    nom = "oto_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(pg_dsn, autocommit=True) as root:
        root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    pool_avant = _conn._pool
    _conn._pool = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", dsn)
        mp.setenv("OTO_TENANT_PRIMAIRE_SLUG", slug)
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
        yield {"dsn": pg_module_dsn, A: semer(c, A, cle=CLES.source),
               B: semer(c, B, cle=CLES.source)}


@pytest.fixture(scope="module")
def export_a(source, tmp_path_factory):
    chemin = tmp_path_factory.mktemp("bout") / "a.jsonl"
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        manifeste = exporter(c, [source[A]["org"]], chemin, transporter_secrets=True)
    return chemin, manifeste


@pytest.fixture(scope="module")
def cible(source, export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            rapport = importer(c, export_a[0], cles=CLES)
        yield {"dsn": dsn, "rapport": rapport}
    finally:
        _detruire(pg_dsn, dsn)


def test_le_fichier_emporte_les_secrets_chiffres_jamais_en_clair(export_a):
    chemin, manifeste = export_a
    assert manifeste["secrets"] == {"connector_credentials": 4, "runner_triggers": 1,
                                    "transcription_jobs": 1}
    assert "clair-" not in chemin.read_text(encoding="utf-8")


def test_rien_d_autrui_et_rien_d_oublie_sur_la_cible(source, cible):
    assert _marquees(cible["dsn"], B) == {}
    assert _marquees(cible["dsn"], A) == _marquees(source["dsn"], A)


def test_le_rapport_d_import_suit_le_manifeste(cible, export_a):
    _, manifeste = export_a
    attendus = {t: v["lignes"] for t, v in manifeste["tables"].items() if v.get("lignes")}
    assert {t: v["lignes"] for t, v in cible["rapport"].items()} == attendus


def test_le_tenant_du_proprietaire_est_la_ligne_1(cible):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        tenants = c.execute("SELECT id, slug, name FROM tenants").fetchall()
        assert tenants == [{"id": 1, "slug": slug_de(A), "name": f"tenant {A}"}]
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
                    CLES.cible, ligne[colonne], aad(ligne))
                with pytest.raises(RuntimeError, match="indéchiffrable"):
                    decrypt_with_key(CLES.source, ligne[colonne], aad(ligne))
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


def test_les_sequences_sont_recalees(cible, export_a):
    _, manifeste = export_a
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        for cle, maximum in manifeste["sequences"].items():
            sequence = c.execute("SELECT pg_get_serial_sequence(%s, %s) AS s",
                                 tuple(cle.split("."))).fetchone()["s"]
            dernier = c.execute(f"SELECT last_value FROM {sequence}").fetchone()["last_value"]
            assert dernier >= maximum, cle


def test_une_cible_deja_peuplee_refuse(cible, export_a):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        with pytest.raises(ImportRefuse, match="pas vierge"):
            importer(c, export_a[0], cles=CLES)


def test_une_cible_d_un_autre_tenant_refuse(export_a, pg_dsn):
    dsn = _naitre(pg_dsn, "autre-instance")
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            with pytest.raises(ImportRefuse, match="tenant primaire"):
                importer(c, export_a[0], cles=CLES)
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)


def test_un_fichier_modifie_ou_sans_cles_refuse(export_a, tmp_path, pg_dsn):
    chemin, _ = export_a
    lignes = chemin.read_text(encoding="utf-8").splitlines(keepends=True)
    i = next(i for i, x in enumerate(lignes) if A in x)
    lignes[i] = lignes[i].replace(A, "Z0000")
    abime = tmp_path / "abime.jsonl"
    abime.write_text("".join(lignes), encoding="utf-8")
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            with pytest.raises(ImportRefuse, match="empreinte"):
                importer(c, abime, cles=CLES)
            with pytest.raises(ImportRefuse, match="deux clés"):
                importer(c, chemin)
    finally:
        _detruire(pg_dsn, dsn)


def test_le_fichier_n_a_pas_de_ligne_hors_manifeste(export_a):
    chemin, manifeste = export_a
    tables = {json.loads(x)["t"] for x in chemin.read_text(encoding="utf-8").splitlines()[:-1]}
    assert tables <= set(manifeste["ordre"])


def test_une_cible_qui_ne_relit_pas_ce_qui_a_ete_ecrit_annule_tout(export_a, pg_dsn,
                                                                    monkeypatch):
    """La vérification finale mord : un tenant primaire mal repris fait tout annuler."""
    from oto_mcp.export_perimetre import importation
    monkeypatch.setattr(importation, "_ecrire_tenant_primaire", lambda conn, ligne: None)
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            with pytest.raises(importation.VerificationEchouee, match="tenants"):
                importer(c, export_a[0], cles=CLES)
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)

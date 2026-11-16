"""La purge d'un tenant hors de la base : objets du stockage (versions comprises),
archives froides du journal, remise au titulaire, et le passage entier
(`scripts/purger_tenant.py tout`), sur une vraie base et un stockage factice VERSIONNÉ.

L'archive factice porte les pièges du CSV de `COPY` : une valeur NULL (champ vide) à côté
d'une chaîne vide (`""`), un champ sur deux lignes, des guillemets doublés. La réécriture
doit les rendre octet pour octet.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import pathlib
import types

import pytest

psycopg = pytest.importorskip("psycopg")
from botocore.exceptions import ClientError  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre import purge, purge_archives, purge_objets  # noqa: E402
from perimetre_banc import BASE_SOURCE, membre, semer  # noqa: E402

E, F = "E3c7d", "F6a1b"
MOIS = "2001-01"
CLE = f"journal/tool_calls/{MOIS}.csv.gz"
SEAU = "seau"


class FauxS3Versionne:
    """Ce que la purge demande à boto3, sur un seau versionné."""

    def __init__(self, versionne: bool = True):
        self.versions: dict[str, list[tuple[str, bytes]]] = {}
        self.n = 0
        self.versionne = versionne
        self.panne_apres_depot = False

    def get_bucket_versioning(self, Bucket):  # noqa: N803
        return {"Status": "Enabled"} if self.versionne else {}

    def poser(self, cle: str, donnees: bytes) -> None:
        self.n += 1
        self.versions.setdefault(cle, []).append((f"v{self.n}", donnees))

    def upload_file(self, chemin, Bucket, Key):  # noqa: N803
        self.poser(Key, pathlib.Path(chemin).read_bytes())
        if self.panne_apres_depot:
            self.panne_apres_depot = False
            raise RuntimeError("panne entre le dépôt et le registre")

    def get_object(self, Bucket, Key):  # noqa: N803
        if not self.versions.get(Key):
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.versions[Key][-1][1])}

    def delete_object(self, Bucket, Key, VersionId=None):  # noqa: N803
        vs = self.versions.get(Key, [])
        self.versions[Key] = [v for v in vs if VersionId is not None and v[0] != VersionId]
        if not self.versions[Key]:
            del self.versions[Key]

    def get_paginator(self, nom):
        assert nom == "list_object_versions"
        faux = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                vs = [{"Key": k, "VersionId": vid, "Size": len(d), "IsLatest": i == len(l) - 1}
                      for k, l in sorted(faux.versions.items()) if k.startswith(Prefix)
                      for i, (vid, d) in enumerate(l)]
                return [{"Versions": vs}]
        return _P()


def _archive(tenant_org: int, compte: str, voisin_org: int) -> tuple[bytes, list[str], list[str]]:
    """(objet, enregistrements du tenant, autres) — textes bruts, en-tête à part. Les
    autres portent les cas croisés : le compte effectif du tenant dans une org voisine, un
    compte du tenant dans une org voisine, notre support dans une org voisine."""
    entete = "id,sub,effective_sub,org_id,args\n"
    du_tenant = [f'1,{compte},,{tenant_org},"{{""a"":\n1}}"\n',   # org, champ sur 2 lignes
                 f"3,{compte},,,\n",                                # compte, sans org
                 f'4,support,,{tenant_org},""\n']                   # support dans l'org
    autres = ['2,autre,,999999,""\n',                               # chaîne vide
              "5,autre,,,\n",                                       # NULL
              f"6,voisin,{compte},{voisin_org},\n",                 # effectif du tenant
              f"7,{compte},,{voisin_org},\n",                       # compte du tenant ailleurs
              f"8,support,,{voisin_org},\n"]                        # support ailleurs
    corps = (entete + du_tenant[0] + autres[0] + du_tenant[1] + du_tenant[2]
             + "".join(autres[1:]))
    return gzip.compress(corps.encode()), du_tenant, autres


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        semes = {m: semer(c, m) for m in (E, F)}
        for m in (E, F):
            c.execute("DELETE FROM billing_contracts WHERE org_id = %s", (semes[m]["org"],))
        membre(c, semes[F]["org"], f"{semes[F]['slug']}:carol")
        yield {"dsn": pg_module_dsn, **semes}


@pytest.fixture
def s3(base):
    s3 = FauxS3Versionne()
    for m in (E, F):
        for cle, donnees in base[m]["objets"].items():
            s3.poser(cle, b"ancienne " + donnees)
            s3.poser(cle, donnees)
    return s3


def _registre(base, objet: bytes, lignes: int) -> None:
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("DELETE FROM journal_archives WHERE mois = %s", (MOIS,))
        c.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES (%s, %s, %s)",
                  (MOIS, CLE, lignes))


def _tenant(base, m) -> purge_archives.Tenant:
    with psycopg.connect(base["dsn"], row_factory=dict_row, autocommit=True) as c, \
            purge.en_lecture_seule(c):
        lu = purge.lire(c, base[m]["slug"])
    return purge_archives.Tenant(*purge.classeur_du_journal(lu.perimetre))


ENTETE = "id,sub,effective_sub,org_id,args\n"


def _poser_archive(base, s3, m: str, voisin: str) -> tuple[list[str], list[str]]:
    objet, du_tenant, autres = _archive(base[m]["org"], base[m]["alice"], base[voisin]["org"])
    s3.poser(CLE, b"version plus ancienne")
    s3.poser(CLE, objet)
    _registre(base, objet, 8)
    return du_tenant, autres


def test_archives_compter_extraire_reecrire_et_reprendre(base, s3, tmp_path):
    du_tenant, autres = _poser_archive(base, s3, F, E)
    # Un autre mois, quel qu'il soit, sans ligne du tenant : parcouru, compté, intact.
    autre_cle = "journal/tool_calls/2001-02.csv.gz"
    s3.poser(autre_cle, b"ancienne")
    s3.poser(autre_cle, gzip.compress((ENTETE + "".join(autres)).encode()))
    intact = list(s3.versions[autre_cle])
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("DELETE FROM journal_archives WHERE mois = '2001-02'")
        c.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES ('2001-02', %s, 5)",
                  (autre_cle,))
    tenant = _tenant(base, F)
    extrait = str(tmp_path / "extrait")
    try:
        with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
            mois, autre = purge_archives.compter(c, s3, SEAU, tenant)
            assert (mois.total, mois.tenant, mois.reste, mois.versions_anciennes) == (8, 3, 5, 1)
            assert (autre.total, autre.tenant) == (5, 0)        # les cas croisés restent
            # Réécrire sans extraction remise : refusé.
            with pytest.raises(purge_archives.ArchiveDiscordante):
                purge_archives.reecrire(c, s3, SEAU, tenant, [mois], str(tmp_path / "rien"))
            assert purge_archives.extraire(s3, SEAU, tenant, [mois], extrait) == {MOIS: 3}
            texte = gzip.decompress((tmp_path / "extrait" / f"{MOIS}.csv.gz").read_bytes())
            assert texte.decode() == ENTETE + "".join(du_tenant)
            # Rejouée, l'extraction complète est reprise, pas réécrite.
            assert purge_archives.extraire(s3, SEAU, tenant, [mois], extrait) == {MOIS: 3}
            # Panne entre le dépôt et le registre.
            s3.panne_apres_depot = True
            with pytest.raises(RuntimeError, match="panne"):
                purge_archives.reecrire(c, s3, SEAU, tenant, [mois, autre], extrait)
            with pytest.raises(purge_archives.ArchiveDiscordante, match="inscrit 8"):
                purge_archives.compter(c, s3, SEAU, tenant)      # sans l'extraction
            mois, autre = purge_archives.compter(c, s3, SEAU, tenant, extrait)
            assert mois.registre_en_retard and (mois.total, mois.tenant) == (5, 0)
            fait = purge_archives.reecrire(c, s3, SEAU, tenant, [mois, autre], extrait)
            assert fait == {MOIS: {"retirees": 3, "reste": 5, "versions_supprimees": 2,
                                   "reprise": "registre"}}
            reecrit = gzip.decompress(s3.versions[CLE][-1][1]).decode()
            assert reecrit == ENTETE + "".join(autres)            # octet pour octet
            assert len(s3.versions[CLE]) == 1
            assert s3.versions[autre_cle] == intact
            assert c.execute("SELECT lignes FROM journal_archives WHERE mois = %s",
                             (MOIS,)).fetchone()["lignes"] == 5
            mois, _ = purge_archives.compter(c, s3, SEAU, tenant, extrait)
            assert (mois.tenant, mois.versions_anciennes, mois.registre_en_retard) == (0, 0, False)
    finally:
        with psycopg.connect(base["dsn"], autocommit=True) as c:
            c.execute("DELETE FROM journal_archives")
        s3.versions.pop(CLE, None)


def test_archive_qui_ne_porte_pas_le_compte_du_registre_refuse(base, s3):
    objet, _, _ = _archive(base[F]["org"], base[F]["alice"], base[E]["org"])
    s3.poser(CLE, objet)
    _registre(base, objet, 9)
    try:
        with psycopg.connect(base["dsn"], row_factory=dict_row) as c, \
                pytest.raises(purge_archives.ArchiveDiscordante, match="registre en inscrit 9"):
            purge_archives.compter(c, s3, SEAU, _tenant(base, F))
    finally:
        with psycopg.connect(base["dsn"], autocommit=True) as c:
            c.execute("DELETE FROM journal_archives")


def test_un_seau_non_versionne_refuse_la_reecriture(base, tmp_path):
    s3 = FauxS3Versionne(versionne=False)
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c, \
            pytest.raises(purge_archives.ArchiveDiscordante, match="pas versionné"):
        purge_archives.reecrire(c, s3, SEAU, _tenant(base, F), [], str(tmp_path))


def test_objets_par_prefixes_toutes_versions_puis_rien(base, s3):
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c, \
            purge.en_lecture_seule(c):
        lu = purge.lire(c, base[F]["slug"])
        prefixes, hors = purge.objets(c, lu, BASE_SOURCE)
    du_projet = f"project-files/{base[F]['projet']}/abc/g.txt"
    pas_lui = f"project-files/{base[F]['projet']}0/abc/g.txt"     # le `/` final protège
    s3.poser(du_projet, b"x")
    s3.poser(pas_lui, b"y")
    versions = purge_objets.inventaire(s3, SEAU, prefixes)
    sous_prefixe = [k for k in base[F]["objets"] if any(k.startswith(p) for p in prefixes)]
    assert purge_objets.resume(versions)["versions"] == 2 * len(sous_prefixe) + 1
    assert hors == {f"project-files/{F}/abc/f.txt"}
    purge_objets.supprimer(s3, SEAU, versions)
    assert purge_objets.inventaire(s3, SEAU, prefixes) == []
    assert pas_lui in s3.versions and f"project-files/{F}/abc/f.txt" in s3.versions
    assert all(k in s3.versions for k in base[E]["objets"])   # le voisin est intact
    with pytest.raises(ValueError):
        purge_objets.inventaire(s3, SEAU, {"project-files/1"})


def _script(monkeypatch, s3):
    chemin = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "purger_tenant.py"
    spec = importlib.util.spec_from_file_location("purger_tenant_banc", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    faux = types.SimpleNamespace(LOCK_KEY=0x07C11, _s3_client=lambda: s3,
                                 _env=lambda nom: SEAU)
    monkeypatch.setattr(mod, "_archivage", lambda: faux)
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    monkeypatch.setenv("DATABASE_URL", "")
    return mod


@pytest.fixture
def script(base, monkeypatch, s3):
    mod = _script(monkeypatch, s3)
    monkeypatch.setenv("DATABASE_URL", base["dsn"])
    return mod


def test_extraire_remet_le_journal_et_les_lignes_tardives_sans_secret(base, tmp_path, script):
    f = base[F]
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("INSERT INTO user_api_tokens (sub, token_hash) VALUES (%s, 'hache-banc')",
                  (f["alice"],))
        c.execute("INSERT INTO runner_triggers (org_id, sub, label, procedure, kind, tools, "
                  "hook_signing_secret_enc) VALUES (%s, %s, 'hook', 'p', 'webhook', '[]', "
                  "'secret-chiffre-banc')", (f["org"], f["alice"]))
    assert script.main(["extraire", "--tenant", f["slug"], "--dossier", str(tmp_path),
                        "--depuis", "2000-01-01T00:00:00Z"]) == 0
    journal = gzip.decompress((tmp_path / "journal.csv.gz").read_bytes()).decode()
    assert journal.count(F) >= 1
    manifeste = json.loads((tmp_path / "tardives" / "MANIFESTE.json").read_text())
    assert manifeste["tables"]["datastore_rows"] == 2
    assert "user_api_tokens" not in manifeste["tables"]
    tout_le_texte = "".join(p.read_text() for p in (tmp_path / "tardives").iterdir())
    assert "hache-banc" not in tout_le_texte and "secret-chiffre-banc" not in tout_le_texte
    assert manifeste["tables"]["runner_triggers"] == 1


def test_tout_en_un_passage_avec_constat_puis_rien(base, tmp_path, script, s3):
    _poser_archive(base, s3, E, F)
    slug, dossier = base[E]["slug"], str(tmp_path / "passage")
    # Sans compte à blanc sur ce dossier, pas de passage.
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 2
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier]) == 0      # à blanc
    assert all(k in s3.versions for k in base[E]["objets"])
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 0
    constat = json.loads((tmp_path / "passage" / "constat.json").read_text())
    assert constat["complet"], constat
    e = constat["elements"]
    assert e["base"]["supprime"] > 0 and e["base"]["reste"] == 0
    assert e["objets"]["reste"] == 0 and e["objets"]["cles_hors_prefixe_non_touchees"] == 1
    assert e["archives"]["avant"] == {MOIS: 3} and e["archives"]["reste"] == 0
    assert "annuaire" in e and "sauvegardes" in e
    assert f"project-files/{E}/abc/f.txt" in s3.versions               # hors préfixe
    assert not any(k in s3.versions for k in base[E]["objets"] if not k.startswith("project"))
    assert all(k in s3.versions for k in base[F]["objets"])            # le voisin
    # Rejoué sur le même dossier : tout est fait, rien n'est rejoué, le constat tient.
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 0
    # Le tenant parti, un nouveau compte à blanc ne devine pas son périmètre.
    assert script.main(["tout", "--tenant", slug, "--dossier", str(tmp_path / "neuf")]) == 2
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("DELETE FROM journal_archives")


def test_un_releve_perime_refuse_le_passage(base, tmp_path, script):
    from perimetre_banc import org
    f, dossier = base[F], str(tmp_path / "passage")
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM user_api_tokens WHERE sub = %s", (f["alice"],))
        nouvelle = org(c, f"org detachee {F}", f["tenant"])
        membre(c, nouvelle, f["alice"])
    assert script.main(["tout", "--tenant", f["slug"], "--dossier", dossier]) == 0
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("UPDATE orgs SET tenant_id = 1 WHERE id = %s", (nouvelle,))
        c.execute("DELETE FROM org_members WHERE org_id = %s", (nouvelle,))
        avant = c.execute("SELECT count(*) FROM tool_calls").fetchone()[0]
    assert script.main(["tout", "--tenant", f["slug"], "--dossier", dossier, "--appliquer"]) == 2
    etat = json.loads((tmp_path / "passage" / "etat.json").read_text())
    assert etat["fait"] == {}
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == avant
        assert c.execute("SELECT count(*) FROM tenants WHERE slug = %s",
                         (f["slug"],)).fetchone()[0] == 1

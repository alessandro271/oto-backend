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

    def __init__(self):
        self.versions: dict[str, list[tuple[str, bytes]]] = {}
        self.n = 0

    def poser(self, cle: str, donnees: bytes) -> None:
        self.n += 1
        self.versions.setdefault(cle, []).append((f"v{self.n}", donnees))

    def upload_file(self, chemin, Bucket, Key):  # noqa: N803
        self.poser(Key, pathlib.Path(chemin).read_bytes())

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


def _archive(tenant_org: int, slug: str) -> tuple[bytes, list[str], list[str]]:
    """(objet, enregistrements du tenant, autres) — textes bruts, en-tête à part."""
    entete = "id,sub,effective_sub,org_id,args\n"
    du_tenant = [f'1,{slug}:x,,{tenant_org},"{{""a"":\n1}}"\n',   # sub, champ sur 2 lignes
                 f"3,autre,{slug}:y,,\n",                          # effective_sub
                 f'4,autre,,{tenant_org},""\n']                    # org, chaîne vide
    autres = ['2,autre,,999999,""\n', "5,autre,,,\n"]              # chaîne vide, NULL
    corps = entete + du_tenant[0] + autres[0] + du_tenant[1] + du_tenant[2] + autres[1]
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
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        lu = purge.lire(c, base[m]["slug"])
    return purge_archives.Tenant(f"{base[m]['slug']}:",
                                 frozenset(str(o) for o in lu.perimetre.orgs))


def test_archives_compter_extraire_reecrire(base, s3, tmp_path):
    objet, du_tenant, autres = _archive(base[F]["org"], base[F]["slug"])
    s3.poser(CLE, b"version plus ancienne")
    s3.poser(CLE, objet)
    _registre(base, objet, 5)
    # Un autre mois, quel qu'il soit, sans ligne du tenant : parcouru, compté, laissé intact.
    autre_cle = "journal/tool_calls/2001-02.csv.gz"
    s3.poser(autre_cle, b"ancienne")
    s3.poser(autre_cle, gzip.compress(("id,sub,effective_sub,org_id,args\n"
                                       + "".join(autres)).encode()))
    intact = list(s3.versions[autre_cle])
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("DELETE FROM journal_archives WHERE mois = '2001-02'")
        c.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES ('2001-02', %s, 2)",
                  (autre_cle,))
    tenant = _tenant(base, F)
    with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
        mois, autre = purge_archives.compter(c, s3, SEAU, tenant)
        assert (autre.total, autre.tenant) == (2, 0)
        assert (mois.total, mois.tenant, mois.reste, mois.versions_anciennes) == (5, 3, 2, 1)
        # Réécrire sans extraction remise : refusé.
        with pytest.raises(purge_archives.ArchiveDiscordante):
            purge_archives.reecrire(c, s3, SEAU, tenant, [mois], str(tmp_path / "rien"))
        assert purge_archives.extraire(s3, SEAU, tenant, [mois], str(tmp_path)) == {MOIS: 3}
        extrait = gzip.decompress((tmp_path / f"{MOIS}.csv.gz").read_bytes()).decode()
        assert extrait == "id,sub,effective_sub,org_id,args\n" + "".join(du_tenant)
        fait = purge_archives.reecrire(c, s3, SEAU, tenant, [mois, autre], str(tmp_path))
        assert fait == {MOIS: {"retirees": 3, "reste": 2, "versions_supprimees": 2}}
        assert s3.versions[autre_cle] == intact
        reecrit = gzip.decompress(s3.versions[CLE][-1][1]).decode()
        assert reecrit == "id,sub,effective_sub,org_id,args\n" + "".join(autres)  # octet pour octet
        assert len(s3.versions[CLE]) == 1
        assert c.execute("SELECT lignes FROM journal_archives WHERE mois = %s",
                         (MOIS,)).fetchone()["lignes"] == 2
        # Rejoué : plus rien du tenant.
        mois, _ = purge_archives.compter(c, s3, SEAU, tenant)
        assert (mois.tenant, mois.versions_anciennes) == (0, 0)
        c.execute("DELETE FROM journal_archives WHERE mois = '2001-02'")


def test_archive_qui_ne_porte_pas_le_compte_du_registre_refuse(base, s3):
    objet, _, _ = _archive(base[F]["org"], base[F]["slug"])
    s3.poser(CLE, objet)
    _registre(base, objet, 6)
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c, \
            pytest.raises(purge_archives.ArchiveDiscordante, match="registre en inscrit 6"):
        purge_archives.compter(c, s3, SEAU, _tenant(base, F))


def test_objets_toutes_versions_puis_rien(base, s3):
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        cles, prefixes = purge.cles_d_objet(c, base[F]["slug"], BASE_SOURCE)
    versions = purge_objets.inventaire(s3, SEAU, cles, prefixes)
    assert purge_objets.resume(versions)["objets"] == len(base[F]["objets"])
    assert purge_objets.resume(versions)["versions"] == 2 * len(base[F]["objets"])
    purge_objets.supprimer(s3, SEAU, versions)
    assert purge_objets.inventaire(s3, SEAU, cles, prefixes) == []
    assert all(k in s3.versions for k in base[E]["objets"])   # le voisin est intact


def _script(monkeypatch, s3):
    chemin = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "purger_tenant.py"
    spec = importlib.util.spec_from_file_location("purger_tenant_banc", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    faux = types.SimpleNamespace(LOCK_KEY=0x07C11, _s3_client=lambda: s3,
                                 _env=lambda nom: SEAU)
    monkeypatch.setattr(mod, "_archivage", lambda: faux)
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    return mod


def test_extraire_remet_le_journal_et_les_lignes_tardives(base, tmp_path, monkeypatch, s3):
    mod = _script(monkeypatch, s3)
    assert mod.main(["extraire", "--tenant", base[F]["slug"], "--dossier", str(tmp_path),
                     "--depuis", "2000-01-01T00:00:00Z"]) == 0
    journal = gzip.decompress((tmp_path / "journal.csv.gz").read_bytes()).decode()
    assert journal.count(F) >= 1
    manifeste = json.loads((tmp_path / "tardives" / "MANIFESTE.json").read_text())
    assert manifeste["tables"]["datastore_rows"] == 2


def test_tout_en_un_passage_avec_constat_puis_rien(base, tmp_path, monkeypatch, s3):
    objet, _, _ = _archive(base[E]["org"], base[E]["slug"])
    s3.poser(CLE, objet)
    _registre(base, objet, 5)
    mod = _script(monkeypatch, s3)
    slug = base[E]["slug"]
    assert mod.main(["tout", "--tenant", slug]) == 0                      # à blanc
    assert all(k in s3.versions for k in base[E]["objets"])
    dossier = str(tmp_path / "passage")
    assert mod.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 0
    constat = json.loads((tmp_path / "passage" / "constat.json").read_text())
    assert constat["complet"]
    e = constat["elements"]
    assert e["base"]["supprime"] > 0 and e["base"]["reste"] == 0
    assert e["objets"]["supprime"] == 2 * len(base[E]["objets"]) and e["objets"]["reste"] == 0
    assert e["archives"]["avant"] == {MOIS: 3} and e["archives"]["reste"] == 0
    assert "annuaire" in e and "sauvegardes" in e
    assert not any(k in s3.versions for k in base[E]["objets"])
    assert any(k in s3.versions for k in base[F]["objets"])
    # Rejoué sur le même dossier : tout est fait, rien n'est rejoué, le constat tient.
    assert mod.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 0
    # Le tenant parti, un passage sans relevé ne devine pas son périmètre.
    assert mod.main(["tout", "--tenant", slug, "--dossier", str(tmp_path / "neuf")]) == 2

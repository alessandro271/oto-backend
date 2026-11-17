"""La purge d'un tenant hors de la base : objets du stockage (versions comprises),
archives froides du journal, remise au titulaire, et le passage entier
(`scripts/purger_tenant.py tout`), sur une vraie base et un stockage factice qui suit la
gestion de versions de S3 (`Enabled`, `Suspended`, jamais activée).

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
    """Ce que la purge demande à boto3, avec la gestion de versions de S3 : `Enabled`
    (chaque écriture a son versionId), `Suspended` (une écriture crée la version `null` et
    ne remplace qu'une version `null`), `None` (jamais activée : tout est `null`). Une
    suppression sans versionId pose un marqueur (`None` dans la liste) sauf si jamais
    activée."""

    def __init__(self, statut: str | None = "Enabled"):
        self.versions: dict[str, list[tuple[str, bytes | None]]] = {}
        self.n = 0
        self.statut = statut
        self.panne_apres_depot = False
        self.depot_rate = False

    def get_bucket_versioning(self, Bucket):  # noqa: N803
        return {"Status": self.statut} if self.statut else {}

    def poser(self, cle: str, donnees: bytes | None) -> str:
        if self.statut == "Enabled":
            self.n += 1
            vid = f"v{self.n}"
        else:
            vid = "null"
        garde = [v for v in self.versions.get(cle, []) if v[0] != vid]
        self.versions[cle] = [*garde, (vid, donnees)]
        return vid

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        donnees = b"pas du gzip" if self.depot_rate else Body.read()
        vid = self.poser(Key, donnees)
        if self.panne_apres_depot:
            self.panne_apres_depot = False
            raise RuntimeError("panne entre le dépôt et le registre")
        return {"VersionId": vid} if self.statut else {}

    def _version(self, Key, VersionId):  # noqa: N803
        vs = self.versions.get(Key) or []
        if VersionId is None:
            if not vs or vs[-1][1] is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return vs[-1][1]
        for vid, donnees in vs:
            if vid == VersionId and donnees is not None:
                return donnees
        raise ClientError({"Error": {"Code": "NoSuchVersion"}}, "GetObject")

    def get_object(self, Bucket, Key, VersionId=None):  # noqa: N803
        return {"Body": io.BytesIO(self._version(Key, VersionId))}

    def head_object(self, Bucket, Key, VersionId=None):  # noqa: N803
        self._version(Key, VersionId)
        return {}

    def delete_object(self, Bucket, Key, VersionId=None):  # noqa: N803
        if VersionId is None:
            if self.statut:
                self.poser(Key, None)                      # un marqueur, données gardées
            else:
                self.versions.pop(Key, None)
            return
        vs = [v for v in self.versions.get(Key, []) if v[0] != VersionId]
        if vs:
            self.versions[Key] = vs
        else:
            self.versions.pop(Key, None)

    def get_paginator(self, nom):
        assert nom == "list_object_versions"
        faux = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                tout = [({"Key": k, "VersionId": vid, "IsLatest": i == len(l) - 1}, d)
                        for k, l in sorted(faux.versions.items()) if k.startswith(Prefix)
                        for i, (vid, d) in enumerate(l)]
                return [{"Versions": [{**v, "Size": len(d)} for v, d in tout if d is not None],
                         "DeleteMarkers": [v for v, d in tout if d is None]}]
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
    """L'archive, une seule version, écrite quand les versions étaient actives (versionId
    réel), comme sur le stockage avant une suspension."""
    objet, du_tenant, autres = _archive(base[m]["org"], base[m]["alice"], base[voisin]["org"])
    statut, s3.statut = s3.statut, "Enabled"
    s3.poser(CLE, objet)
    s3.statut = statut
    _registre(base, objet, 8)
    return du_tenant, autres


STATUTS = pytest.mark.parametrize("statut", ["Enabled", "Suspended"])


@pytest.fixture
def seau(statut):
    return FauxS3Versionne(statut)


def _lignes(base) -> int:
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        return c.execute("SELECT lignes FROM journal_archives WHERE mois = %s",
                         (MOIS,)).fetchone()[0]


def _vider(base, s3) -> None:
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("DELETE FROM journal_archives")
    s3.versions.pop(CLE, None)


@STATUTS
def test_archives_compter_extraire_reecrire_et_reprendre(base, seau, tmp_path):
    s3 = seau
    du_tenant, autres = _poser_archive(base, s3, F, E)
    ancienne = s3.versions[CLE][0][0]
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
            assert (mois.total, mois.tenant, mois.reste, mois.versions_anciennes) == (8, 3, 5, 0)
            assert (mois.version, mois.refus) == (ancienne, None)
            assert (autre.total, autre.tenant, autre.refus) == (5, 0, None)
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
            nouvelle = s3.versions[CLE][-1][0]
            assert [v for v, _ in s3.versions[CLE]] == [ancienne, nouvelle]
            with pytest.raises(purge_archives.ArchiveDiscordante, match="inscrit 8"):
                purge_archives.compter(c, s3, SEAU, tenant)      # sans l'extraction
            mois, autre = purge_archives.compter(c, s3, SEAU, tenant, extrait)
            assert mois.registre_en_retard and (mois.total, mois.tenant) == (5, 0)
            fait = purge_archives.reecrire(c, s3, SEAU, tenant, [mois, autre], extrait)
            assert fait == {MOIS: {"retirees": 3, "reste": 5, "version_nouvelle": nouvelle,
                                   "versions_supprimees": [ancienne], "reprise": "registre"}}
            assert [v for v, _ in s3.versions[CLE]] == [nouvelle]
            reecrit = gzip.decompress(s3.versions[CLE][-1][1]).decode()
            assert reecrit == ENTETE + "".join(autres)            # octet pour octet
            assert s3.versions[autre_cle] == intact
            assert _lignes(base) == 5
            mois, _ = purge_archives.compter(c, s3, SEAU, tenant, extrait)
            assert (mois.tenant, mois.versions_anciennes, mois.registre_en_retard) == (0, 0, False)
    finally:
        _vider(base, s3)
        s3.versions.pop(autre_cle, None)


@STATUTS
def test_reecrire_verifie_la_nouvelle_puis_supprime_l_ancienne(base, seau, tmp_path, statut):
    """Le même chemin en `Enabled` et en `Suspended` : la nouvelle version est relue par
    son versionId, puis l'ancienne, qui porte encore le tenant, est supprimée par le sien."""
    s3 = seau
    _, autres = _poser_archive(base, s3, F, E)
    ancienne = s3.versions[CLE][0][0]
    tenant, extrait = _tenant(base, F), str(tmp_path / "extrait")
    try:
        with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
            mois = purge_archives.compter(c, s3, SEAU, tenant)
            purge_archives.extraire(s3, SEAU, tenant, mois, extrait)
            fait = purge_archives.reecrire(c, s3, SEAU, tenant, mois, extrait)
        nouvelle = "null" if statut == "Suspended" else s3.versions[CLE][-1][0]
        assert fait == {MOIS: {"retirees": 3, "reste": 5, "version_supprimee": ancienne,
                               "version_nouvelle": nouvelle}}
        assert [v for v, _ in s3.versions[CLE]] == [nouvelle]
        assert gzip.decompress(s3.versions[CLE][0][1]).decode() == ENTETE + "".join(autres)
        assert _lignes(base) == 5
    finally:
        _vider(base, s3)


def _ancienne_courante_null(s3):
    s3.statut = "Suspended"
    objet = s3.versions.pop(CLE)[0][1]
    s3.poser(CLE, objet)                                   # la seule copie, `null`


def _jamais_activee(s3):
    s3.statut = None
    objet = s3.versions.pop(CLE)[0][1]
    s3.poser(CLE, objet)


def _deux_versions(s3):
    s3.poser(CLE, s3.versions[CLE][0][1])


def _marqueur(s3):
    objet = s3.versions[CLE][0][1]
    s3.delete_object(SEAU, CLE)                            # marqueur, non courant ensuite
    s3.poser(CLE, objet)


@pytest.mark.parametrize("preparer, raison", [
    (_ancienne_courante_null, "version courante `null`"),
    (_jamais_activee, "jamais activée"),
    (_deux_versions, "2 versions sur la clé"),
    (_marqueur, "marqueur"),
])
def test_une_archive_sans_filet_est_refusee_avant_toute_ecriture(base, tmp_path, preparer,
                                                                 raison, script, capsys):
    s3 = FauxS3Versionne("Enabled")
    _poser_archive(base, s3, F, E)
    preparer(s3)
    avant = list(s3.versions[CLE])
    tenant, extrait = _tenant(base, F), str(tmp_path / "extrait")
    try:
        with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
            (mois,) = purge_archives.compter(c, s3, SEAU, tenant)
            assert mois.refus and raison in mois.refus
            purge_archives.extraire(s3, SEAU, tenant, [mois], extrait)
            with pytest.raises(purge_archives.ArchiveDiscordante, match="rien n'est écrit"):
                purge_archives.reecrire(c, s3, SEAU, tenant, [mois], extrait)
            # Le compte à blanc du script dit le verdict, la version et la part.
            with purge.en_lecture_seule(c):
                fige = purge.lire(c, base[F]["slug"]).perimetre
            compte = script.compte_archives(c, s3, SEAU, fige)
        assert compte[MOIS]["verdict"].startswith("refusée : ")
        assert raison in compte[MOIS]["verdict"] and compte[MOIS]["tenant"] == 3
        assert compte[MOIS]["version_courante"] == avant[-1][0]
        assert s3.versions[CLE] == avant and _lignes(base) == 8
    finally:
        _vider(base, s3)


@STATUTS
def test_une_nouvelle_version_ratee_garde_l_ancienne(base, seau, tmp_path):
    s3 = seau
    _poser_archive(base, s3, F, E)
    avant = list(s3.versions[CLE])
    ancienne = avant[0][0]
    tenant, extrait = _tenant(base, F), str(tmp_path / "extrait")
    try:
        with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
            mois = purge_archives.compter(c, s3, SEAU, tenant)
            purge_archives.extraire(s3, SEAU, tenant, mois, extrait)
            s3.depot_rate = True
            with pytest.raises(purge_archives.ArchiveDiscordante) as refus:
                purge_archives.reecrire(c, s3, SEAU, tenant, mois, extrait)
        nouvelle = f"v{s3.n}" if s3.statut == "Enabled" else "null"
        assert ancienne in str(refus.value) and f"nouvelle version {nouvelle}" in str(refus.value)
        assert "redevient courante" in str(refus.value)
        assert s3.versions[CLE] == avant and _lignes(base) == 8       # ancienne gardée
    finally:
        _vider(base, s3)


def test_nouvelle_ratee_et_ancienne_illisible_ne_retire_rien(base, tmp_path, monkeypatch):
    s3 = FauxS3Versionne("Suspended")
    _poser_archive(base, s3, F, E)
    tenant, extrait = _tenant(base, F), str(tmp_path / "extrait")
    try:
        with psycopg.connect(base["dsn"], autocommit=True, row_factory=dict_row) as c:
            mois = purge_archives.compter(c, s3, SEAU, tenant)
            purge_archives.extraire(s3, SEAU, tenant, mois, extrait)
            s3.depot_rate = True

            def illisible(**_):
                raise ClientError({"Error": {"Code": "500"}}, "HeadObject")
            monkeypatch.setattr(s3, "head_object", illisible)
            with pytest.raises(purge_archives.ArchiveDiscordante, match="PAS retirée"):
                purge_archives.reecrire(c, s3, SEAU, tenant, mois, extrait)
        assert [v for v, _ in s3.versions[CLE]] == [mois[0].version, "null"]
        assert _lignes(base) == 8
    finally:
        _vider(base, s3)


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


def test_objets_en_suspended_chaque_version_par_son_id_sans_marqueur(base):
    """En `Suspended`, supprimer sans versionId poserait un marqueur et laisserait les
    données : chaque version, `null` comprise, part par son versionId."""
    s3 = FauxS3Versionne("Enabled")
    cle = f"avatars/{base[F]['alice']}/a.png"
    s3.poser(cle, b"avant la suspension")
    s3.statut = "Suspended"
    s3.poser(cle, b"apres")                                     # version `null`
    versions = purge_objets.inventaire(s3, SEAU, {f"avatars/{base[F]['alice']}/"})
    assert sorted(v.version for v in versions) == ["null", "v1"]
    assert purge_objets.supprimer(s3, SEAU, versions) == 2
    assert cle not in s3.versions                               # ni version ni marqueur
    with pytest.raises(ValueError, match="sans versionId"):
        purge_objets.supprimer(s3, SEAU, [purge_objets.Version(cle, None, 0)])


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


def test_tout_en_un_passage_avec_constat_puis_rien(base, tmp_path, script, s3, capsys):
    _poser_archive(base, s3, E, F)
    slug, dossier = base[E]["slug"], str(tmp_path / "passage")
    # Sans compte à blanc sur ce dossier, pas de passage.
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 2
    ancienne = s3.versions[CLE][0][0]
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier]) == 0      # à blanc
    assert f"{MOIS} : version {ancienne} — acceptée — 3 ligne(s) à retirer" in \
        capsys.readouterr().out
    assert all(k in s3.versions for k in base[E]["objets"])
    assert script.main(["tout", "--tenant", slug, "--dossier", dossier, "--appliquer"]) == 0
    constat = json.loads((tmp_path / "passage" / "constat.json").read_text())
    assert constat["complet"], constat
    e = constat["elements"]
    assert e["base"]["supprime"] > 0 and e["base"]["reste"] == 0
    assert e["objets"]["reste"] == 0 and e["objets"]["cles_hors_prefixe_non_touchees"] == 1
    assert e["archives"]["avant"] == {MOIS: 3} and e["archives"]["reste"] == 0
    nouvelle = s3.versions[CLE][-1][0]
    assert e["archives"]["fait"] == {MOIS: {"retirees": 3, "reste": 5,
                                            "version_supprimee": ancienne,
                                            "version_nouvelle": nouvelle}}
    assert [v for v, _ in s3.versions[CLE]] == [nouvelle]
    assert (f"{MOIS} : 3 ligne(s) retirée(s) ; ancienne version supprimée {ancienne} ; "
            f"nouvelle version {nouvelle}") in capsys.readouterr().out
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

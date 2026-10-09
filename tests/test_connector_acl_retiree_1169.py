"""`connector_acl` retirée par une révision, APRÈS son export attesté (oto-backend#1169).

```
base NEUVE                     le démarrage ne crée plus la table ; la révision ne fait rien
table non vide, sans export    la révision REFUSE (ExportNonAtteste), la table reste
export → relecture → révision  l'objet relu porte chaque ligne, l'attestation est posée,
                               la révision supprime ; rejouée, elle ne fait rien
table modifiée après export    la révision refuse : on réexporte, on ne supprime pas
table vide                     elle part sans attestation (rien à perdre)
```

L'export n'écrase jamais un objet (bucket non versionné) et n'affiche aucune ligne.
"""
from __future__ import annotations

import csv
import gzip
import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

RACINE = Path(__file__).resolve().parent.parent
AVANT, REVISION = "0052_shadow_l7_cle_origine", "0053_connector_acl_retiree"
BUCKET = "seau"
INSTANT = datetime(2026, 10, 10, 8, 30, 0, tzinfo=timezone.utc)
CLE = "archives/connector_acl/2026-10-10T083000Z.csv.gz"

# La table telle que la production la porte avant ce lot.
_DDL_D_AVANT = """
CREATE TABLE connector_acl (
    scope_type TEXT NOT NULL CHECK (scope_type IN ('org', 'group')),
    scope_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('group', 'user')),
    principal_id TEXT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_id, connector, principal_type, principal_id)
);
INSERT INTO connector_acl (scope_type, scope_id, connector, principal_type, principal_id,
                           granted_by, granted_at) VALUES
  ('org', '11', 'hunter', 'user', 'usr_a', 'usr_admin', '2026-09-15 10:00+02'),
  ('org', '11', 'hunter', 'group', '7', NULL, '2026-09-01 09:00+00'),
  ('org', '12', 'linear', 'user', 'usr_b', 'usr_admin', '2026-08-20 12:00+00');
"""


# ── Sans base ─────────────────────────────────────────────────────────────────

def test_plus_aucun_code_servi_ne_nomme_la_table():
    """Hors du registre des révisions, la table n'existe plus dans le paquet : ni DDL du
    démarrage (une base neuve la recréerait), ni lecture, ni classement d'export."""
    porteurs = sorted(
        p.relative_to(RACINE).as_posix()
        for p in (RACINE / "oto_mcp").rglob("*.py")
        if "/migrations/" not in p.as_posix()
        and "connector_acl" in p.read_text(encoding="utf-8")
        # Commentaires d'histoire seulement (la restriction disparue le 24/09/2026).
        and p.name not in ("chain_resolution.py", "chain_shadow.py", "_init.py"))
    assert not porteurs, f"`connector_acl` encore nommée hors des révisions : {porteurs}"


def test_deplacer_cles_org_ne_lit_plus_la_table():
    source = (RACINE / "scripts" / "deplacer_cles_org.py").read_text(encoding="utf-8")
    assert "FROM connector_acl" not in source


# ── Faux Object Storage ───────────────────────────────────────────────────────

class FauxS3:
    def __init__(self):
        self.objets: dict[str, bytes] = {}
        self.ecritures: list[str] = []

    def head_object(self, Bucket, Key):
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objets[Key])}

    def put_object(self, Bucket, Key, Body):
        self.ecritures.append(Key)
        self.objets[Key] = bytes(Body)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objets[Key])}


def _script():
    chemin = RACINE / "deploy" / "archive_connector_acl.py"
    spec = importlib.util.spec_from_file_location("archive_connector_acl_1169", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Sur une vraie base ────────────────────────────────────────────────────────

def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _a_la_table(dsn: str) -> bool:
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass('connector_acl') IS NOT NULL").fetchone()[0]


def _version(dsn: str) -> str:
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _sql(dsn: str, requete: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(requete)


def _exporter(dsn: str, s3: FauxS3, sortie: list) -> int:
    import psycopg
    with psycopg.connect(dsn) as conn:
        return _script().archiver(conn, s3, BUCKET, out=sortie.append, maintenant=INSTANT)


@pytest.fixture
def base(live, pg_module_dsn):
    _sql(pg_module_dsn, "DROP TABLE IF EXISTS connector_acl")
    yield pg_module_dsn
    _sql(pg_module_dsn, "DROP TABLE IF EXISTS connector_acl")


def _monter(cfg):
    from alembic import command
    command.stamp(cfg, AVANT)
    command.upgrade(cfg, REVISION)


def test_une_base_neuve_n_a_pas_la_table_et_la_revision_ne_fait_rien(base):
    assert not _a_la_table(base), "le démarrage ne doit plus créer la table"
    _monter(_alembic())
    assert not _a_la_table(base)
    assert _version(base) == REVISION


def test_sans_export_la_revision_refuse_et_la_table_reste(base):
    from oto_mcp.db.export_avant_retrait import ExportNonAtteste
    _sql(base, _DDL_D_AVANT)
    with pytest.raises(ExportNonAtteste, match="3 ligne.*archive_connector_acl"):
        _monter(_alembic())
    assert _a_la_table(base), "la révision a supprimé une table non exportée"
    assert _version(base) == AVANT


def test_export_relecture_puis_revision_qui_supprime_puis_rejeu_sans_effet(base):
    _sql(base, _DDL_D_AVANT)
    s3, sortie = FauxS3(), []
    assert _exporter(base, s3, sortie) == 0, sortie
    assert s3.ecritures == [CLE]
    lignes = list(csv.reader(io.StringIO(gzip.decompress(s3.objets[CLE]).decode())))
    assert lignes[0][:5] == ["scope_type", "scope_id", "connector", "principal_type",
                             "principal_id"]
    assert len(lignes) == 1 + 3
    texte = "\n".join(sortie)
    assert "3 lignes relues" in texte and f"s3://{BUCKET}/{CLE}" in texte
    assert "usr_a" not in texte and "usr_b" not in texte, "une ligne affichée"
    # Rejoué sur une table attestée inchangée : rien à refaire, rien de déposé.
    sortie2 = []
    assert _exporter(base, s3, sortie2) == 0
    assert "déjà exportée et inchangée" in "\n".join(sortie2)
    assert s3.ecritures == [CLE]

    cfg = _alembic()
    _monter(cfg)
    assert not _a_la_table(base), "la révision n'a rien retiré"
    assert _version(base) == REVISION
    from oto_mcp.db import init_db
    init_db()
    assert not _a_la_table(base), "le démarrage a reposé la table"
    _monter(cfg)                                    # rejouée : rien à faire, rien ne lève
    assert not _a_la_table(base)


def test_table_modifiee_apres_export_la_revision_refuse(base):
    from oto_mcp.db.export_avant_retrait import ExportNonAtteste
    _sql(base, _DDL_D_AVANT)
    assert _exporter(base, FauxS3(), []) == 0
    _sql(base, "UPDATE connector_acl SET granted_by = 'autre' WHERE principal_id = 'usr_b'")
    with pytest.raises(ExportNonAtteste, match="a changé depuis son export.*différente"):
        _monter(_alembic())
    assert _a_la_table(base)
    _sql(base, "INSERT INTO connector_acl (scope_type, scope_id, connector, principal_type, "
               "principal_id) VALUES ('org', '13', 'http', 'user', 'usr_c')")
    with pytest.raises(ExportNonAtteste, match="3 ligne.*attestée.*4 aujourd'hui"):
        _monter(_alembic())
    assert _a_la_table(base)


def test_le_fuseau_de_session_ne_change_pas_l_empreinte(base):
    import psycopg

    from oto_mcp.db import export_avant_retrait as att
    _sql(base, _DDL_D_AVANT)
    releves = []
    for fuseau in ("UTC", "Europe/Paris", "America/New_York"):
        with psycopg.connect(base) as c:
            c.execute(f"SET TimeZone = '{fuseau}'")
            releves.append(att.releve(lambda q: c.execute(q).fetchone()[0], "connector_acl"))
            assert c.execute("SELECT current_setting('TimeZone')").fetchone()[0] == fuseau
    assert len(set(releves)) == 1 and releves[0][0] == 3


def test_table_vide_part_sans_attestation_et_sans_export(base):
    _sql(base, _DDL_D_AVANT.split("INSERT")[0])
    s3, sortie = FauxS3(), []
    assert _exporter(base, s3, sortie) == 0
    assert "vide" in "\n".join(sortie) and not s3.ecritures
    _monter(_alembic())
    assert not _a_la_table(base)


def test_l_export_n_ecrase_jamais_un_objet(base):
    _sql(base, _DDL_D_AVANT)
    s3, sortie = FauxS3(), []
    s3.objets[CLE] = b"archive d'avant"
    assert _exporter(base, s3, sortie) == 1
    assert "jamais écrasée" in "\n".join(sortie)
    assert s3.objets[CLE] == b"archive d'avant" and not s3.ecritures
    from oto_mcp.db import export_avant_retrait as att
    import psycopg
    with psycopg.connect(base) as c:
        assert att.lire(lambda q: c.execute(q).fetchone()[0], "connector_acl") is None


def test_une_relecture_differente_n_atteste_rien(base):
    _sql(base, _DDL_D_AVANT)

    class S3Tronque(FauxS3):
        def put_object(self, Bucket, Key, Body):
            super().put_object(Bucket, Key, bytes(Body)[:-5])

    sortie = []
    assert _exporter(base, S3Tronque(), sortie) == 1
    assert "relu différent" in "\n".join(sortie)
    from oto_mcp.db.export_avant_retrait import ExportNonAtteste
    with pytest.raises(ExportNonAtteste):
        _monter(_alembic())


def test_le_retour_arriere_leve_et_ne_touche_a_rien(base):
    from alembic import command
    cfg = _alembic()
    command.stamp(cfg, REVISION)
    with pytest.raises(RuntimeError, match="irréversible"):
        command.downgrade(cfg, AVANT)
    assert _version(base) == REVISION

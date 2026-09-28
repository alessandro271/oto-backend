"""La naissance d'une base : sous l'identité que l'instance DÉCLARE, et sur des prérequis
vérifiés (#969, ADR 0070 §7.2).

Contre un PostgreSQL réel, chaque cas sur sa base jetable, réellement vide au départ :

1. **une instance qui n'est pas nous** démarre sur une base vide : schéma monté,
   version posée à la tête du registre, tenant 1 = celui qu'elle déclare — et rien
   de notre identité dedans ;
2. **notre base** (tenant 1 `oto`) démarre sous notre déclaration sans rien changer,
   le nom de marque déclaré (`OTO_BRAND_NAME`) n'écrasant pas le sien ;
3. **une base qui porte un autre tenant primaire** que celui déclaré refuse le
   démarrage, et la transaction n'a rien laissé ;
4. **une instance sans déclaration** refuse avant d'avoir rien écrit ;
5. **les prérequis** : un rôle sans droit de créer dans le schéma, ou sans droit de
   créer une extension exigée, ou un serveur qui ne la propose pas — refus nommé,
   jamais l'erreur PostgreSQL brute au milieu du boot.

La moitié « versions » du critère de l'issue (une révision non vide ne s'applique pas à
une base neuve, s'applique encore à une base ancienne) est le banc
`tests/test_boot_pose_la_version.py`.
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from _base_jetable import base_jetable  # noqa: E402


def _tables(ouvrir) -> int:
    with ouvrir() as c:
        return c.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relkind IN ('r', 'p')").fetchone()[0]


def _tenants(ouvrir) -> list[tuple]:
    with ouvrir() as c:
        return [tuple(r) for r in c.execute("SELECT id, slug, name FROM tenants ORDER BY id")]


def _declarer(monkeypatch, slug: str, nom: str) -> None:
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", slug)
    monkeypatch.setenv("OTO_BRAND_NAME", nom)


def test_une_instance_tierce_nait_sous_sa_declaration(pg_dsn, monkeypatch):
    from oto_mcp import db, tenancy
    from oto_mcp.db._version_alembic import tete_du_registre

    _declarer(monkeypatch, "acme", "Acme")
    with base_jetable(pg_dsn) as ouvrir:
        assert _tables(ouvrir) == 0
        db.init_db()
        assert _tables(ouvrir) > 50, "le schéma n'est pas monté"
        with ouvrir() as c:
            assert [r[0] for r in c.execute("SELECT version_num FROM alembic_version")] \
                == [tete_du_registre()]
        assert _tenants(ouvrir) == [(1, "acme", "Acme")], "notre tenant est né chez un tiers"
        # Le tenant déclaré EST le primaire, pour tout le code : ses comptes restent nus,
        # et une org née sur cette base est la sienne.
        assert tenancy.primary_slug() == "acme"
        assert tenancy.qualify("acme", "abc123") == "abc123"
        with ouvrir() as c:
            org = c.execute("INSERT INTO orgs (name) VALUES ('une org') RETURNING id, tenant_id"
                            ).fetchone()
        assert org[1] == 1
        assert db.org_tenant_slug(org[0]) == "acme"
        # Un second démarrage est un no-op : rien ne se re-sème.
        db.init_db()
        assert _tenants(ouvrir) == [(1, "acme", "Acme")]


def test_notre_base_demarre_inchangee_sous_notre_declaration(pg_dsn, monkeypatch):
    from oto_mcp import db

    with base_jetable(pg_dsn) as ouvrir:
        db.init_db()                                # déclaration de la suite : oto / oto
        assert _tenants(ouvrir) == [(1, "oto", "oto")]
        with ouvrir() as c:
            c.execute("INSERT INTO tenants (slug, name) VALUES ('acme', 'Acme')")
        # Le nom n'est posé qu'à la naissance : une base existante garde le sien.
        _declarer(monkeypatch, "oto", "Un autre nom")
        db.init_db()
        assert _tenants(ouvrir) == [(1, "oto", "oto"), (2, "acme", "Acme")]
        with ouvrir() as c:
            nouveau = c.execute("INSERT INTO tenants (slug, name) VALUES ('b', 'B') "
                                "RETURNING id").fetchone()[0]
        assert nouveau == 3, "la séquence des tenants n'est plus calée"


def test_une_base_qui_porte_un_autre_tenant_primaire_refuse_le_demarrage(pg_dsn, monkeypatch):
    from oto_mcp import db
    from oto_mcp.db._tenant_primaire import TenantPrimaireDiscordant

    with base_jetable(pg_dsn) as ouvrir:
        db.init_db()                                # née sous `oto`
        _declarer(monkeypatch, "acme", "Acme")
        with pytest.raises(TenantPrimaireDiscordant, match="'oto'.*'acme'"):
            db.init_db()
        assert _tenants(ouvrir) == [(1, "oto", "oto")]


@pytest.mark.parametrize("variable", ["OTO_TENANT_PRIMAIRE_SLUG", "OTO_BRAND_NAME"])
def test_une_instance_sans_declaration_ne_nait_pas(pg_dsn, monkeypatch, variable):
    from oto_mcp import db, identite_instance

    monkeypatch.delenv(variable)
    with pytest.raises(identite_instance.IdentiteNonDeclaree, match=variable):
        identite_instance.verifier()                # le refus du démarrage (`server.main`)
    with base_jetable(pg_dsn) as ouvrir:
        with pytest.raises(RuntimeError, match=variable):
            db.init_db()
        assert _tables(ouvrir) == 0, "une base est née sans identité déclarée"


def test_un_slug_declare_invalide_est_refuse(monkeypatch):
    from oto_mcp import tenancy

    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "Acme:SAS")
    with pytest.raises(tenancy.TenantPrimaireNonDeclare, match="n'est pas un slug"):
        tenancy.primary_slug()


# ── Prérequis ───────────────────────────────────────────────────────────────────

@pytest.fixture
def role_restreint(pg_dsn):
    """Un rôle de connexion SANS privilège particulier (ni superutilisateur, ni
    propriétaire de la base) — créé pour le test, retiré après."""
    nom, mdp = "oto_restreint_" + uuid.uuid4().hex[:8], uuid.uuid4().hex
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(f"CREATE ROLE {nom} LOGIN PASSWORD '{mdp}'")
    yield nom, mdp
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(f"DROP ROLE IF EXISTS {nom}")


def test_un_role_qui_ne_peut_pas_creer_dans_le_schema_est_refuse(pg_dsn, role_restreint):
    from oto_mcp import db
    from oto_mcp.db._prerequis import PrerequisBaseManquant

    with base_jetable(pg_dsn, role=role_restreint) as ouvrir:
        with ouvrir() as c:
            c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        with pytest.raises(PrerequisBaseManquant, match="droit CREATE sur le schéma 'public'"):
            db.init_db()
        assert _tables(ouvrir) == 0


def test_un_role_qui_ne_peut_pas_creer_l_extension_est_refuse(pg_dsn, role_restreint):
    from oto_mcp import db
    from oto_mcp.db._prerequis import PrerequisBaseManquant

    with base_jetable(pg_dsn, role=role_restreint) as ouvrir:
        with ouvrir() as c:
            c.execute(f"GRANT CREATE ON SCHEMA public TO {role_restreint[0]}")
        with pytest.raises(PrerequisBaseManquant,
                           match="`vector`.*n'a pas le droit de la créer"):
            db.init_db()
        assert _tables(ouvrir) == 0
        # `vector` posée par un administrateur : c'est `pg_trgm` qui manque ensuite.
        with ouvrir() as c:
            c.execute("CREATE EXTENSION vector")
        with pytest.raises(PrerequisBaseManquant,
                           match="`pg_trgm`.*n'a pas le droit de la créer"):
            db.init_db()
        # Posées une fois par un administrateur, la même base naît avec ce même rôle.
        with ouvrir() as c:
            c.execute("CREATE EXTENSION pg_trgm")
        db.init_db()
        assert _tables(ouvrir) > 50


def test_un_serveur_sans_l_extension_est_refuse(pg_dsn, monkeypatch):
    from oto_mcp import db
    from oto_mcp.db import _prerequis

    # Un serveur qui ne la propose pas : on ne peut pas retirer une extension du serveur
    # de test, on en exige donc une qu'aucun serveur ne propose.
    monkeypatch.setitem(_prerequis.EXTENSIONS, "oto_extension_absente", "le banc")
    with base_jetable(pg_dsn) as ouvrir:
        with pytest.raises(_prerequis.PrerequisBaseManquant, match="n'est pas disponible"):
            db.init_db()
        assert _tables(ouvrir) == 0

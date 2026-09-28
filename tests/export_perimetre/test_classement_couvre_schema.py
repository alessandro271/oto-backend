"""Le classement du périmètre couvre EXACTEMENT le schéma réel (oto-backend#1088).

Le schéma est celui que monte `init_db` sur une base neuve (fixture `live`), jamais une
reconstitution : les colonnes posées par `ALTER` au démarrage n'existent que là.

Ce test est le garde-fou de l'export : une table ajoutée au schéma sans être classée
dans `oto_mcp/export_perimetre/classement.py` le fait rougir — sans lui, l'export
raterait cette table en silence, et la cible naîtrait amputée sans que personne le voie.
Chaque refus est prouvé en lui PRÉSENTANT l'anomalie qu'il prétend attraper.
"""
from __future__ import annotations

import dataclasses

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre import classement as cl  # noqa: E402
from oto_mcp.export_perimetre.decouverte import (  # noqa: E402
    ClassementIncomplet, lire_schema, verifier_classement)
from oto_mcp.export_perimetre.extraction import ordre_d_export  # noqa: E402
from oto_mcp.export_perimetre.regles import ParOrg, ParSub, Via  # noqa: E402


@pytest.fixture(scope="module")
def conn(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        yield c


@pytest.fixture(scope="module")
def schema(conn):
    return lire_schema(conn)


def _refus(schema, classement) -> str:
    with pytest.raises(ClassementIncomplet) as e:
        verifier_classement(schema, classement)
    return str(e.value)


def test_le_classement_couvre_le_schema_reel(schema):
    assert len(schema.colonnes) > 90, "le schéma lu n'est pas celui du démarrage"
    verifier_classement(schema, cl.CLASSEMENT)


def test_une_table_ajoutee_au_schema_sans_classement_est_refusee(conn):
    conn.execute("CREATE TABLE table_neuve_1088 (id BIGSERIAL PRIMARY KEY, org_id BIGINT)")
    try:
        message = _refus(lire_schema(conn), cl.CLASSEMENT)
    finally:
        conn.execute("DROP TABLE table_neuve_1088")
    assert "table `table_neuve_1088` non classée" in message


def test_une_table_retiree_du_classement_est_refusee(schema):
    classement = {t: e for t, e in cl.CLASSEMENT.items() if t != "docs"}
    assert "table `docs` non classée" in _refus(schema, classement)


def test_une_entree_sans_table_est_refusee(schema):
    classement = {**cl.CLASSEMENT, "table_disparue": cl.possedee(ParOrg())}
    assert "entrée `table_disparue`" in _refus(schema, classement)


def test_une_regle_qui_vise_une_colonne_absente_est_refusee(schema):
    classement = {**cl.CLASSEMENT, "users": cl.possedee(ParSub("sub_renomme"))}
    assert "`users` : la colonne `sub_renomme`" in _refus(schema, classement)


def test_un_lien_declare_fk_sans_cle_etrangere_est_refuse(schema):
    classement = {**cl.CLASSEMENT,
                  "docs": cl.indirecte(Via("projects", ("parent_id",)))}
    assert "aucune clé étrangère" in _refus(schema, classement)


def test_un_heritage_d_une_table_qui_ne_part_pas_est_refuse(schema):
    classement = {**cl.CLASSEMENT,
                  "tenant_admins": cl.indirecte(Via("tenants", ("slug",), ("slug",)))}
    assert "`tenant_admins` hérite de `tenants`" in _refus(schema, classement)


def test_une_table_indirecte_ne_porte_pas_de_regle_directe(schema):
    classement = {**cl.CLASSEMENT, "org_groups": cl.indirecte(ParOrg())}
    assert "`org_groups` (indirecte)" in _refus(schema, classement)


def test_une_exclusion_ou_une_table_d_instance_dit_pourquoi(schema):
    muette = dataclasses.replace(cl.CLASSEMENT["billing_payments"], raison="")
    classement = {**cl.CLASSEMENT, "billing_payments": muette,
                  "platform_instructions": cl.Table(cl.INSTANCE)}
    message = _refus(schema, classement)
    assert "`billing_payments` (exclue) doit dire pourquoi" in message
    assert "`platform_instructions` (instance) doit dire pourquoi" in message


def test_les_colonnes_secretes_et_hors_base_existent(schema):
    for t, e in cl.CLASSEMENT.items():
        for c in (*e.secrets, *e.hors_base):
            assert c in schema.colonnes[t], f"{t}.{c}"


def test_l_ordre_d_export_place_chaque_parent_avant_ses_enfants(schema):
    ordre = ordre_d_export(schema, cl.CLASSEMENT)
    rang = {t: i for i, t in enumerate(ordre)}
    assert set(ordre) == {t for t, e in cl.CLASSEMENT.items() if e.classe in cl.EXPORTEES}
    for t in ordre:
        for k in schema.cles_de(t):
            if k.cible in rang and k.cible != t:
                assert rang[k.cible] < rang[t], f"{k.cible} doit précéder {t}"

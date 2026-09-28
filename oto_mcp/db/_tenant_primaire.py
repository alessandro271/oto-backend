"""Le tenant primaire d'une base : semé à sa naissance, tel que l'instance le déclare (#969).

La ligne 1 de `tenants` est le tenant de l'instance elle-même — celui dont les subs
restent nus, et auquel toute org est rattachée par défaut. Le démarrage la
semait sous NOTRE identité (une constante) : toute base neuve naissait avec notre
tenant, faux dès la première seconde sur l'instance d'un tiers (ADR 0070 §7.2).

Elle se sème désormais depuis la déclaration de l'instance — son slug
(`tenancy.primary_slug`, `OTO_TENANT_PRIMAIRE_SLUG`) et le nom de sa marque
(`email_brand.nom_instance`, `OTO_BRAND_NAME` : un seul fait, une seule variable) —, et
une base existante est CONFRONTÉE à cette déclaration :

- **base sans ligne 1** (neuve) : la ligne est posée avec le slug et le nom déclarés ;
- **ligne 1 au slug déclaré** : rien ne change — c'est le cas de notre base, qui
  déclare le slug qu'elle porte ;
- **ligne 1 à un autre slug** : refus du démarrage. Tout le code classe les comptes
  et les orgs par ce slug ; servir une base sous le slug d'une autre instance
  rangerait chaque compte chez un tenant qui n'est pas le sien, sans une erreur.

Le NOM n'est posé qu'à la naissance : c'est un libellé, pas une identité, et une base
existante garde le sien.
"""
from __future__ import annotations

import logging

import psycopg

from .. import tenancy

logger = logging.getLogger(__name__)

_ID_PRIMAIRE = 1


class TenantPrimaireDiscordant(RuntimeError):
    """La ligne 1 de `tenants` ne porte pas le slug que l'instance déclare."""


def semer(conn: psycopg.Connection) -> None:
    """Pose la ligne 1 si elle manque, puis la confronte à la déclaration. À appeler
    dans la transaction du démarrage, après la création de `tenants`."""
    # Import tardif : la marque tire l'email, qui tire la base.
    from ..email_brand import nom_instance
    slug, nom = tenancy.primary_slug(), nom_instance()
    pose = conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        (_ID_PRIMAIRE, slug, nom),
    ).fetchone()
    if pose:
        logger.info("tenant primaire semé : %r (%s)", slug, nom)
    en_base = conn.execute("SELECT slug FROM tenants WHERE id = %s",
                           (_ID_PRIMAIRE,)).fetchone()["slug"]
    if en_base != slug:
        raise TenantPrimaireDiscordant(
            f"la base porte le tenant primaire {en_base!r} (ligne {_ID_PRIMAIRE} de "
            f"`tenants`), l'instance déclare {slug!r} (OTO_TENANT_PRIMAIRE_SLUG). Une "
            "base ne change pas de tenant primaire par un réglage : déclarer celui "
            "qu'elle porte, ou pointer l'instance sur sa propre base.")
    # La séquence ne bouge pas sur un INSERT à id explicite : sans ce recalage, le
    # prochain tenant naîtrait sur l'id 1 et casserait (cf. « ids fusionnés = la
    # MÊME séquence », docs/live-migrations.md).
    conn.execute("SELECT setval(pg_get_serial_sequence('tenants','id'), "
                 "GREATEST((SELECT MAX(id) FROM tenants), 1))")

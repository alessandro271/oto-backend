"""connector_acl retirée, après export attesté (oto-backend#1169).

`connector_acl` réservait un connecteur à une partie des membres d'une org. La
restriction a disparu le 24/09/2026 (ADR 0053 D1 : restreindre un connecteur, c'est
placer la clé au bon niveau) : plus aucun code ne lit ni n'écrit la table. Son dernier
lecteur, `scripts/deplacer_cles_org.py --vers equipe`, prend désormais la liste des
membres en argument (`--membres`). Elle porte encore des lignes : elles sont exportées
AVANT le retrait (`deploy/archive_connector_acl.py`), et la révision le vérifie.

**Ce qu'elle fait.** Elle lit le catalogue (`to_regclass`, aucun verrou) et ne fait
RIEN si la table est absente — base neuve (le démarrage ne la crée plus), base déjà
nettoyée, rejeu. Sinon, sous `lock_timeout` 5 s :
1. `LOCK TABLE connector_acl IN ACCESS EXCLUSIVE MODE` — plus personne n'écrit entre la
   vérification et le retrait ;
2. `export_avant_retrait.exiger` : table vide, ou attestation d'export (commentaire de
   la table posé par le script d'export après relecture de l'objet) dont le compte et
   l'empreinte sont ceux de la table à cet instant. Sinon `ExportNonAtteste`, qui nomme
   la commande d'export ; la transaction est annulée, rien n'est supprimé ;
3. `DROP TABLE connector_acl` — sa clé primaire et ses deux CHECK partent avec elle ;
   aucune clé étrangère vers elle ni depuis elle.

**Verrou** (seulement là où la table existe) : `AccessExclusiveLock` sur
`connector_acl` seule, bref — un relevé de quelques dizaines de lignes, une écriture de
catalogue. Aucun lecteur ni écrivain ne l'attend : la table n'a plus de code.

L'essai à blanc (`--sql`), sans base à lire, imprime le verrou et l'ordre ; la
vérification de l'export n'existe qu'en ligne. Ne jamais rejouer cette sortie à la main.

**Ordre : export, puis tag, puis cette révision.** L'ancien code recrée la table au
démarrage (`CREATE TABLE IF NOT EXISTS`) : retirée avant que la production serve le
code de ce lot, un redémarrage la reposerait vide — sans dommage (une table vide part
sans attestation au rejeu), mais la base se dirait déjà en `0053`.

**Retour arrière : refusé.** Le contenu est dans l'export ; recréer la table vide ne
rendrait rien et ne servirait aucun code. `downgrade` lève.

Révision : 0053_connector_acl_retiree
Précédente : 0052_shadow_l7_cle_origine
"""
from __future__ import annotations

from alembic import context, op
from sqlalchemy import text

from oto_mcp.db import export_avant_retrait

revision = "0053_connector_acl_retiree"
down_revision = "0052_shadow_l7_cle_origine"
branch_labels = None
depends_on = None

TABLE = "connector_acl"
EXPORT = ("`deploy/archive_connector_acl.py`, par le lanceur : docs/connector-vault.md "
          "§« Retirer connector_acl »")

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_VERROU = f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE"
_TABLE_PRESENTE = text(f"SELECT to_regclass('{TABLE}') IS NOT NULL")

DDL = f"DROP TABLE {TABLE}"


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute(_ATTENTE_MAX)
        op.execute(_VERROU)
        op.execute(DDL)
        return
    bind = op.get_bind()
    if not bind.execute(_TABLE_PRESENTE).scalar():
        return
    op.execute(_ATTENTE_MAX)
    op.execute(_VERROU)
    export_avant_retrait.exiger(lambda sql: bind.exec_driver_sql(sql).scalar(), TABLE, EXPORT)
    op.execute(DDL)


def downgrade() -> None:
    raise RuntimeError(
        "0053_connector_acl_retiree est irréversible : `connector_acl` n'a plus ni lecteur "
        "ni écrivain, et son contenu est dans l'export attesté. Pour descendre sous 0053, "
        "`alembic stamp 0052_shadow_l7_cle_origine` (la table absente ne gêne aucune "
        "révision antérieure).")

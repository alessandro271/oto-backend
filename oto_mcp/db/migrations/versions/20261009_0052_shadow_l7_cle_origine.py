"""access_shadow_l7 : la clé du compteur L7 étendue à l'ORIGINE — ce que faisait un script manuel (#1169).

Le compteur de la double lecture L7 (`db/access_shadow.py`) distingue l'environnement
qui écrit (`origine`, prod et préprod partageant la base). Sa clé primaire `(day,
connector, org_id, classe)` ne le distinguait pas ; l'étendre était confié au script
manuel `scripts/migrate_shadow_origine.py` — un geste qui ne se rejoue sur aucune autre
base (docs/migrations-versionnees.md §5.3). Cette révision produit EXACTEMENT son état
final, et le script est retiré :

- l'index unique `access_shadow_l7_origine_key (day, connector, org_id, classe, origine)
  NULLS NOT DISTINCT` (`access_shadow.CLE_ORIGINE`) — une clé primaire refuse le NULL,
  et les lignes d'origine inconnue doivent rester uniques entre elles ;
- PUIS le retrait de `access_shadow_l7_pkey`. Dans cet ordre : à aucun instant la table
  n'est sans garde d'unicité (un compteur dédoublé ne se répare pas). Aucun dédoublonnage
  n'est nécessaire : l'ancienne clé, plus étroite, garantit l'unicité de la nouvelle.

Les lignes existantes ne sont pas réécrites (celles d'avant la colonne gardent `origine`
NULL : elles sont réellement ambiguës).

**Ce qu'elle fait.** Constats au catalogue, sans verrou sur la table (`pg_attribute`,
`pg_index` par `to_regclass`, `pg_constraint`) :

- la colonne `origine` manque → lève : le démarrage la pose depuis août 2026, une base
  qui ne l'a pas n'a pas démarré le code de cette révision ;
- l'index : déjà valide → rien ; INVALIDE → `IndexInvalide` ; absent → construit
  CONCURRENTLY sous le verdict commun (`db/index_concurrent.py`), puis `indisvalid`
  exigé ; table au-delà du seuil (100 000 lignes estimées) → `ConstructionManuelleRequise`
  et le geste manuel du §5.1 (`oto-mcp maintenance index-concurrents 0052`, puis la
  révision rejouée) ;
- la clé primaire : présente → `ALTER TABLE access_shadow_l7 DROP CONSTRAINT IF EXISTS
  access_shadow_l7_pkey` ; absente → rien.

Sur une base NEUVE (le démarrage pose l'index et plus la clé primaire) ou déjà migrée
par l'ancien script : aucun ordre, aucun verrou. Rejouée : rien. L'essai à blanc
(`--sql`) imprime les deux ordres.

**Verrous** (seulement là où il reste à faire) : la construction prend un
`ShareUpdateExclusiveLock` sur `access_shadow_l7` — ni lectures ni écritures bloquées —
et attend la fin des transactions plus anciennes qu'elle (`lock_timeout` 5 min) ; durée
proportionnelle à la table (quelques milliers de lignes attendues : bien moins d'une
seconde). Le retrait de la clé prend un `AccessExclusiveLock` sur `access_shadow_l7`
seule, bref (écriture de catalogue et suppression de l'index de la clé), attente bornée
par `lock_timeout` 5 s. Aucune autre table n'est touchée (aucune clé étrangère).

**Ordre avec le code : AVANT la fusion.** Le code de ce lot n'a plus de repli pour
l'ancienne clé : sous elle, une seconde origine pour la même (jour, connecteur, org,
classe) LÈVE (`bump_shadow`, best-effort côté appelant : l'observation est perdue et
journalisée). L'ancien code, lui, tolère les deux formes de clé : jouer la révision avant
le tag est sûr. Prod et préprod partagent la base : jouée une fois, elle sert les deux.

**Retour arrière : refusé.** Sous la clé étendue, deux origines ont chacune leur ligne
pour la même (jour, connecteur, org, classe) : l'ancienne clé primaire ne se reposerait
pas sans fusionner — c'est-à-dire mélanger — ces compteurs. `downgrade` lève.

Révision : 0052_shadow_l7_cle_origine
Précédente : 0051_guides_retiree
"""
from __future__ import annotations

from alembic import context, op
from sqlalchemy import text

from oto_mcp.db import index_concurrent
from oto_mcp.db.access_shadow import CLE_ORIGINE, REVISION_CLE_ORIGINE

revision = REVISION_CLE_ORIGINE
down_revision = "0051_guides_retiree"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: Les constats, au catalogue : `regclass` lève si la table manque.
_COLONNE_PRESENTE = text(
    "SELECT 1 FROM pg_attribute WHERE attrelid = 'access_shadow_l7'::regclass "
    "AND attname = 'origine' AND NOT attisdropped")
_CLE_PRIMAIRE_PRESENTE = text(
    "SELECT 1 FROM pg_constraint WHERE conrelid = 'access_shadow_l7'::regclass "
    "AND conname = 'access_shadow_l7_pkey' AND contype = 'p'")

DDL_CLE = "ALTER TABLE access_shadow_l7 DROP CONSTRAINT IF EXISTS access_shadow_l7_pkey"


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc n'a pas de base à lire : les ordres tels qu'ils partiraient
        # sur une base qui porte encore l'ancienne clé, l'index d'abord.
        with op.get_context().autocommit_block():
            op.execute(CLE_ORIGINE.ddl_concurrent)
        op.execute(_ATTENTE_MAX)
        op.execute(DDL_CLE)
        return
    if op.get_bind().execute(_COLONNE_PRESENTE).first() is None:
        raise RuntimeError(
            "access_shadow_l7.origine est absente : le démarrage la pose depuis août 2026. "
            "Démarrer une fois le code de cette révision sur cette base, puis la rejouer.")
    index_concurrent.poser_par_revision(op, CLE_ORIGINE)
    if op.get_bind().execute(_CLE_PRIMAIRE_PRESENTE).first() is None:
        return
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_CLE)


def downgrade() -> None:
    raise RuntimeError(
        "0052_shadow_l7_cle_origine est irréversible : sous la clé étendue, deux origines "
        "ont chacune leur ligne, et reposer `access_shadow_l7_pkey` exigerait de mélanger "
        "leurs compteurs. Pour descendre sous 0052, `alembic stamp 0051_guides_retiree` "
        "(la clé étendue ne gêne aucune révision antérieure).")

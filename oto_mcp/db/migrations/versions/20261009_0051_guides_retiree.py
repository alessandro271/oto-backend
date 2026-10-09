"""guides retirée : le `DROP TABLE guides` que le lot 2 d'oto#239 laissait à un geste manuel (#1169).

La table `guides` n'a plus ni lecteur ni écrivain depuis le 23/09/2026 (oto#239, lot 1) :
les guides sont des nœuds depuis le lot M1, et les 23 lignes mesurées en production le
01/09/2026 y étaient toutes déjà, aucune plus récente que son nœud. Son retrait était
confié à « l'opérationnel » par un commentaire de démarrage — un geste qui ne se rejoue
sur aucune autre base (docs/migrations-versionnees.md §5.3).

**Ce qu'elle fait.** Deux constats, puis seulement ce qui reste à faire :

- `to_regclass('guides')` (catalogue, aucun verrou) : si la table est là,
  `DROP TABLE IF EXISTS guides`. Ses index (clé primaire, unique `(scope, owner_id,
  slug)`, `idx_guides_fts`/`idx_guides_trgm` s'ils subsistent) partent avec elle. Sans
  `CASCADE` : aucune clé étrangère ni vue ne la vise ; s'il en restait une, l'ordre
  lève plutôt que d'emporter un objet servi ;
- une lecture d'`aux_embeddings` (`kind = 'guide'`, par sa clé primaire) : s'il reste
  des embeddings de ce genre, `DELETE`. Leur `ref` est un `guides.id`, et plus rien ne
  les lit — la recherche sémantique lit le genre `node` depuis #282 (`db/aux_embed.py`).

Sur une base NEUVE (le démarrage ne crée plus la table) ou déjà nettoyée : aucun ordre,
aucun verrou au-delà de la lecture. Rejouée, elle ne fait rien. L'essai à blanc
(`--sql`), sans base à lire, imprime les deux ordres.

**Verrous** (seulement là où il reste à faire) : `AccessExclusiveLock` sur `guides`
seule, bref — une écriture de catalogue et la suppression de ses fichiers (quelques
dizaines de lignes) ; `RowExclusiveLock` sur `aux_embeddings` pour le `DELETE`, qui ne
bloque ni les lectures ni les écritures des autres lignes. Attente bornée par
`lock_timeout` 5 s : une transaction qui tient encore la table fait échouer la révision
plutôt que d'empiler des requêtes derrière elle (la rejouer suffit).

**Ordre avec le code : indifférent.** Aucun code servi depuis le 23/09/2026 ne nomme la
table ni le genre `guide`, et son démarrage ne la recrée pas.

**Retour arrière : refusé.** La table est morte, ses lignes vivent dans `nodes` ; la
recréer vide ne rendrait rien. `downgrade` lève.

Révision : 0051_guides_retiree
Précédente : 0050_journal_totaux_jour
"""
from __future__ import annotations

from alembic import context, op
from sqlalchemy import text

revision = "0051_guides_retiree"
down_revision = "0050_journal_totaux_jour"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: Les constats : `to_regclass` rend NULL sans lever ni verrouiller ; l'`EXISTS` lit
#: la clé primaire `(kind, ref)` d'`aux_embeddings` (`AccessShareLock`, une lecture).
_TABLE_PRESENTE = text("SELECT to_regclass('guides') IS NOT NULL")
_EMBEDDINGS_PRESENTS = text(
    "SELECT EXISTS (SELECT 1 FROM aux_embeddings WHERE kind = 'guide')")

DDL_TABLE = "DROP TABLE IF EXISTS guides"
PURGE_EMBEDDINGS = "DELETE FROM aux_embeddings WHERE kind = 'guide'"


def upgrade() -> None:
    # L'essai à blanc (`--sql`) n'a pas de base à lire : il imprime les ordres tels
    # qu'ils partiraient sur une base qui porte encore la table et ses embeddings.
    if context.is_offline_mode():
        a_faire = [DDL_TABLE, PURGE_EMBEDDINGS]
    else:
        bind = op.get_bind()
        a_faire = ([DDL_TABLE] if bind.execute(_TABLE_PRESENTE).scalar() else []) + \
                  ([PURGE_EMBEDDINGS] if bind.execute(_EMBEDDINGS_PRESENTS).scalar() else [])
    if not a_faire:
        return
    op.execute(_ATTENTE_MAX)
    for ordre in a_faire:
        op.execute(ordre)


def downgrade() -> None:
    raise RuntimeError(
        "0051_guides_retiree est irréversible : `guides` n'a plus ni lecteur ni "
        "écrivain et ses lignes vivent dans `nodes` ; la recréer ne rendrait aucune "
        "donnée. Pour descendre sous 0051, `alembic stamp 0050_journal_totaux_jour` "
        "(la table absente ne gêne aucune révision antérieure).")

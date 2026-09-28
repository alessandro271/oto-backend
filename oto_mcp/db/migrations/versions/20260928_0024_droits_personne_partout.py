"""org_entitlements : la portée « personne, toutes orgs » — `org_id` nullable, et une
ligne sans org ni personne refusée par la base — oto-backend#1089.

Un droit déclaré peut appartenir à une personne quelle que soit l'org où elle agit
(`docs/droits-declares.md`). Trois portées : l'org (`org_id` posé, `sub` NULL), une
personne dans l'org (les deux posés), une personne partout (`org_id` NULL, `sub` posé).
Deux gestes, ADDITIFS pour le code en place :

1. `CHECK (org_id IS NOT NULL OR sub IS NOT NULL)` (`org_entitlements_une_portee`),
   posé d'abord, tant que `org_id` est encore NOT NULL : chaque ligne existante le
   satisfait par construction ;
2. `ALTER COLUMN org_id DROP NOT NULL`.

L'unicité `org_entitlements_une_ligne` (`UNIQUE NULLS NOT DISTINCT (org_id, sub,
right_key, source)`, 0014) couvre déjà la nouvelle portée : deux lignes `(NULL, sub,
droit, source)` sont égales pour elle. Elle ne bouge pas.

**Verrous.** Les deux ordres prennent `AccessExclusiveLock` sur `org_entitlements`
SEULE (ni la clé étrangère vers `orgs` ni une autre table ne sont touchées), tenu
jusqu'à la fin de la transaction. `DROP NOT NULL` est une écriture de catalogue ; le
`CHECK` parcourt la table une fois pour se valider (une poignée de lignes). L'attente
du verrou est bornée par `lock_timeout` 5 s : au-delà, la révision abandonne sans rien
écrire plutôt que de faire attendre les lecteurs de droits.

**Ordre : AVANT la fusion** de préférence. L'ancien code ne pose que des lignes à
`org_id` posé (le CHECK ne refuse rien qu'il écrive) et ne lit que par `org_id = …`
(une ligne à `org_id` NULL lui est invisible). Le code du lot LIT la nouvelle portée
sans elle (aucune ligne ne peut alors l'avoir) ; seule la POSE d'une ligne de cette
portée échoue sans elle, en `NotNullViolation` (500), jamais en silence.

⚠️ Suppose la 0015 jouée : tant que la clé primaire `(org_id, right_key, source)`
existe, `DROP NOT NULL` échoue de lui-même (« column is in a primary key »).

Idempotente : une base NEUVE a déjà la forme (fragment `db/schema/entitlements.py`) —
le CHECK est sauté, `DROP NOT NULL` sur une colonne déjà nullable ne fait rien. Pas au
démarrage : une base servie ne reçoit la forme que d'ici.

Retour arrière : retire le CHECK et repose `NOT NULL` — il **échoue de lui-même** tant
qu'une ligne de la portée personne partout existe (rien n'est supprimé en douce) :

    SELECT count(*) FROM org_entitlements WHERE org_id IS NULL;

Révision : 0024_droits_personne_partout
Précédente : 0023_signature_webhook
"""
from __future__ import annotations

from alembic import op

revision = "0024_droits_personne_partout"
down_revision = "0023_signature_webhook"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'org_entitlements_une_portee') THEN
        ALTER TABLE org_entitlements ADD CONSTRAINT org_entitlements_une_portee
            CHECK (org_id IS NOT NULL OR sub IS NOT NULL);
    END IF;
END $$""")
    op.execute("ALTER TABLE org_entitlements ALTER COLUMN org_id DROP NOT NULL")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE org_entitlements ALTER COLUMN org_id SET NOT NULL")
    op.execute("ALTER TABLE org_entitlements "
               "DROP CONSTRAINT IF EXISTS org_entitlements_une_portee")

"""Plusieurs comptes par canal de messagerie hébergée : la PK d'`unipile_accounts` prend
l'emplacement.

Le lot A (code, au démarrage) a posé la colonne `slot` (`''` = le compte principal, celui
de toute ligne existante) et l'index unique `(sub, org_id, provider, slot)` sur lequel le
code neuf arbitre ses `ON CONFLICT`. Il a laissé la PK `(sub, org_id, provider)` : le
code d'avant arbitre dessus. Tant qu'elle est là, un second compte sur un même canal ne
peut pas naître (`db.unipile.slots_ouverts()` le lit, l'ajout refuse
`multi_account_unavailable`).

**Ce qui change.** La PK devient `unipile_accounts_slot_pkey (sub, org_id, provider,
slot)` — le nom de la PK d'une base neuve (`db/schema/unipile.py`), pour qu'un
`DROP CONSTRAINT` futur vise la même chose partout. Aucune ligne ne bouge : toutes ont
`slot = ''`, la nouvelle clé est aussi unique que l'ancienne. Idempotente : sur une base
dont la PK porte déjà `slot`, rien n'est fait.

**Ce qui ne change PAS.** Les prêts (`connector_account_grants`, `…_group_grants`)
gardent leur PK : un bénéficiaire reçoit UN compte par canal d'un même prêteur ; prêter un
autre compte du canal au même bénéficiaire remplace le premier. L'index
`uq_unipile_accounts_slot` reste (le démarrage le repose de toute façon) : un doublon de
la PK, sur une petite table.

**Verrous.** `AccessExclusiveLock` sur `unipile_accounts`, le temps de construire l'index
de la PK (une table de quelques centaines de lignes). `lock_timeout` 5 s : la résolution
de chaque appel LinkedIn/WhatsApp la lit ; un échec net, qu'on rejoue, plutôt qu'une
file d'attente derrière le verrou.

⚠️ **Ordre avec le code : APRÈS LE TAG** (`docs/migrations-versionnees.md` §5.1). Prod et
préprod partagent la base. Le code d'avant écrit `ON CONFLICT (sub, org_id, provider)` :
jouée pendant qu'il sert encore, cette révision lui retire son arbitre, et chaque
(re)connexion d'un compte y lèverait. D'où : fusion -> tag de prod portant le lot A ->
`oto-mcp migrer upgrade head` -> l'ajout d'un compte s'ouvre de lui-même (le processus
relit la PK au premier ajout demandé).

Retour arrière : rend la PK `(sub, org_id, provider)` — refusé (lève) s'il existe des
comptes ajoutés (`slot <> ''`) : les supprimer est une décision, pas un effet de bord.

Révision : 20261118_1500_unipile_slots
Précédente : 20261118_1200_apps_custom_api
"""
from __future__ import annotations

from alembic import op

revision = "20261118_1500_unipile_slots"
down_revision = "20261118_1200_apps_custom_api"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

# La PK porte-t-elle déjà l'emplacement ? (base neuve, ou révision déjà jouée)
_PK_A_LE_SLOT = """
SELECT EXISTS (
  SELECT 1 FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
  WHERE i.indrelid = 'unipile_accounts'::regclass AND i.indisprimary
    AND a.attname = 'slot')
"""

UPGRADE = f"""
ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS slot TEXT NOT NULL DEFAULT '';
DO $$
BEGIN
  IF NOT ({_PK_A_LE_SLOT}) THEN
    ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_pkey;
    ALTER TABLE unipile_accounts
      ADD CONSTRAINT unipile_accounts_slot_pkey PRIMARY KEY (sub, org_id, provider, slot);
  END IF;
END $$;
"""

DOWNGRADE = f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM unipile_accounts WHERE slot <> '') THEN
    RAISE EXCEPTION 'unipile_accounts porte des comptes ajoutés (slot <> ''''), la PK '
      '(sub, org_id, provider) ne peut pas revenir : les déconnecter et les supprimer '
      'd''abord est une décision';
  END IF;
  IF ({_PK_A_LE_SLOT}) THEN
    ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_slot_pkey;
    ALTER TABLE unipile_accounts
      ADD CONSTRAINT unipile_accounts_pkey PRIMARY KEY (sub, org_id, provider);
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DOWNGRADE)

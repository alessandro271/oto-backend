"""selection_org_reelle : une sélection de connecteur vit sous une org réelle, jamais sous `0`.

oto-backend#959. `user_selected_connectors` et `connector_selection_removed` gardaient
des lignes sous l'ancienne sentinelle `org_id = 0` (« perso sans org », retirée par
l'ADR 0030 §8) : aucune surface ne les lit, elles étaient invisibles. Le code n'y écrit
plus (947a59c4 : `select`/`pause`/`unselect` refusent en `no_active_org` sans org
active) ; cette révision le rend IMPOSSIBLE, en base :

1. `CHECK (org_id > 0)` — `user_selected_connectors_org_reelle` et
   `connector_selection_removed_org_reelle` ;
2. `ALTER COLUMN org_id DROP DEFAULT` sur les deux : une écriture qui oublie l'org
   échoue (`NotNullViolation`) au lieu de se ranger sous `0`.

`connector_selection_seeded` n'est PAS touchée : elle garde `0` pour les sentinelles
des migrations de démarrage (`_BACKFILL_MARK` et les marques de split).

**⚠️ Elle ÉCHOUE tant qu'une ligne sous `0` reste** — c'est voulu : le `CHECK` est
posé VALIDÉ, jamais `NOT VALID`, pour qu'une ligne invisible ne survive pas en silence
sous une contrainte qui prétend le contraire. À jouer APRÈS le nettoyage, livré par
947a59c4 (`scripts/selections_org_zero.py`, via le lanceur) :

    python scripts/selections_org_zero.py --details                     # à blanc
    python scripts/selections_org_zero.py --appliquer --restes purger --purger-orphelins

puis vérifier :

    SELECT count(*) FROM user_selected_connectors   WHERE org_id <= 0;   -- 0
    SELECT count(*) FROM connector_selection_removed WHERE org_id <= 0;  -- 0

Le script et son banc sont retirés dans le MÊME commit que cette révision : la
contrainte rend leur entrée impossible. Ce commit part donc dans un tag POSTÉRIEUR à
celui de 947a59c4, une fois `--appliquer` joué.

**Verrous.** Chaque `ADD CONSTRAINT … CHECK` prend `AccessExclusiveLock` sur SA table
et la parcourt une fois pour se valider ; `DROP DEFAULT` est une écriture de catalogue.
Les deux tables comptent quelques milliers de lignes (une par couple compte × org ×
connecteur) : la validation dure quelques millisecondes, d'où un `CHECK` validé d'un
coup plutôt que `NOT VALID` puis `VALIDATE` (la décomposition n'a d'intérêt que pour
une table dont le parcours tient le verrou longtemps). L'attente du verrou est bornée
par `lock_timeout` 5 s : au-delà, la révision abandonne sans rien écrire plutôt que de
faire attendre les lectures de sélection (chaque `list_tools`) — on la rejoue.

**Ordre avec le code : indifférent.** Le code en place n'écrit plus sous `0` depuis
947a59c4 ; l'ancien code (avant 947a59c4) le pouvait pour un compte sans org active —
cette écriture échouerait alors, en erreur nommée, au lieu de créer une ligne
invisible.

Idempotente : une base NEUVE a déjà la forme (`connectors/selection.py::_SCHEMA`) —
chaque `CHECK` est sauté s'il existe, `DROP DEFAULT` sur une colonne sans défaut ne
fait rien. Pas au démarrage : une base servie ne reçoit la forme que d'ici.

Retour arrière : retire les deux `CHECK` et repose `DEFAULT 0`.

Révision : 0031_selection_org_reelle
Précédente : 0030_file_de_travail_ordre
"""
from __future__ import annotations

from alembic import op

revision = "0031_selection_org_reelle"
down_revision = "0030_file_de_travail_ordre"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: (table, contrainte) — la même forme que le `CREATE TABLE` des bases neuves.
_TABLES = (
    ("user_selected_connectors", "user_selected_connectors_org_reelle"),
    ("connector_selection_removed", "connector_selection_removed_org_reelle"),
)


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for table, contrainte in _TABLES:
        op.execute(f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{contrainte}') THEN
        ALTER TABLE {table} ADD CONSTRAINT {contrainte} CHECK (org_id > 0);
    END IF;
END $$""")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN org_id DROP DEFAULT")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for table, contrainte in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN org_id SET DEFAULT 0")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {contrainte}")

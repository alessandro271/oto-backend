"""Budget d'unités : barème, modes de payeur, soldes, réservations et mouvements.

Des tables NEUVES, nées entières — le DDL est le fragment `db/schema/credits.py::CREDITS`,
exécuté tel quel : `credit_scales`, `credit_scale_lines`, `credit_payer_modes`,
`credit_balances`, `credit_holds`, `credit_movements` et leurs index. Aucun montant en
monnaie, aucune devise, aucun prix, aucune facture ni moyen de paiement
(`docs/credits-au-moment-de-l-appel.md`).

**Verrous.** `AccessExclusiveLock` sur les tables neuves seules (vides, instantané) ; la
clé étrangère de `credit_payer_modes` prend un `ShareRowExclusiveLock` bref sur `orgs`
(les lectures passent) : `lock_timeout` 5 s, un échec net qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes tables (`CREATE TABLE
IF NOT EXISTS`, même fragment), y compris sur une base existante ; rien ne les lit
avant le moteur du budget, livré éteint ou en `report`. Prod et préprod partagent la
base : posées une fois, elles servent les deux. En production, la révision se joue par
`oto-mcp migrer upgrade head` après le tag.

Retour arrière : retire les tables — elles portent les versements du commerce, à
relire par son curseur AVANT.

Révision : 0068_credits
Précédente : 0067_runner_jobs_declencheurs
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.credits import CREDITS

revision = "0068_credits"
down_revision = "0067_runner_jobs_declencheurs"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_TABLES = ("credit_movements", "credit_holds", "credit_balances", "credit_payer_modes",
           "credit_scale_lines", "credit_scales")


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(CREDITS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")

"""Repli d'un abonnement épuisé vers la clé API de l'org : l'interrupteur, la cause.

Trois colonnes additives, le fragment `db/schema/runs.py::MODEL_SUBSCRIPTION_REPLI`
exécuté tel quel (le démarrage le joue aussi, sous le garde des DDL) :

- `org_model_subscription_modes.repli_api BOOLEAN NOT NULL DEFAULT FALSE` — le repli se
  CHOISIT, org par org ; toute ligne existante reste fermée ;
- `user_model_subscriptions.limit_epuise BOOLEAN NOT NULL DEFAULT FALSE` et
  `limit_utilisation DOUBLE PRECISION` (NULL) — ce qui a posé la pause d'un
  abonnement. Une pause écrite avant eux n'ouvre aucun repli.

`ADD COLUMN … DEFAULT` constant ne réécrit pas la table (PG ≥ 11) ; les deux tables
comptent une ligne par org réglée et par personne abonnée. Verrous sous
`lock_timeout` : un échec net, qu'on rejoue.

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit aucune des trois
colonnes : jouer cette révision avant le déploiement ne change rien pour lui. Le code
du lot, lui, les LIT à chaque réservation d'un travail d'abonnement (`get_mode`, par
`en_pool`) et à chaque rapport de forfait (`marquer_statut`) : à jouer AVANT la fusion,
pour fermer la fenêtre où un démarrage raté sur `lock_timeout` les laisserait
absentes. Le retour arrière retire les trois colonnes ; le code qui les lit doit être
retiré avant lui.

Révision : 0025_repli_api
Précédente : 0024_droits_personne_partout
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.runs import MODEL_SUBSCRIPTION_REPLI

revision = "0025_repli_api"
down_revision = "0024_droits_personne_partout"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(MODEL_SUBSCRIPTION_REPLI)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE user_model_subscriptions DROP COLUMN IF EXISTS limit_utilisation")
    op.execute("ALTER TABLE user_model_subscriptions DROP COLUMN IF EXISTS limit_epuise")
    op.execute("ALTER TABLE org_model_subscription_modes DROP COLUMN IF EXISTS repli_api")

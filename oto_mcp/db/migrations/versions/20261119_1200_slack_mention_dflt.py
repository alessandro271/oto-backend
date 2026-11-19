"""Hors d'un DM, l'app Slack ne répond qu'à qui la tague — désormais PAR DÉFAUT, pour
toutes les orgs (`orgs.slack_mention_only`, `db/slack_reglage.py`).

Décision du 19/11/2026 (Julien) : le réglage d'org de `20261109_2100_slack_mention_only`
reste, mais son défaut passe de FALSE à TRUE, et les orgs existantes y passent aussi.
Une réponse non taguée dans un fil d'agent (ou de l'agent intégré) ne relance plus rien ;
un admin d'org qui veut l'ancien comportement le repose (`oto_org_settings domain=slack
op=set mention_only=false`). Les DM ne changent jamais.

**Ce qui change.** `ALTER COLUMN … SET DEFAULT TRUE` (catalogue seul), puis
`UPDATE orgs SET slack_mention_only = TRUE WHERE NOT slack_mention_only` : aucune org
n'a pu choisir FALSE en connaissance de cause (c'était le défaut, jamais posé
explicitement depuis le 09/11), elles passent toutes à TRUE.

**Verrous.** `AccessExclusiveLock` bref pour le défaut, puis un `UPDATE` de quelques
centaines de lignes sur `orgs`. `lock_timeout` 5 s : `orgs` est lue à chaque requête ;
un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le code d'avant (#1613) lit déjà la colonne : la
révision change le comportement dès qu'elle est jouée, avec ou sans le tag.
Idempotente : rejouée, l'UPDATE ne touche rien.

Retour arrière : rend le défaut FALSE et remet TOUTES les orgs à FALSE (l'état d'avant :
le choix d'une org qui avait posé TRUE avant cette révision n'est pas distinguable).

Révision : 20261119_1200_slack_mention_dflt
Précédente : 20261118_1500_unipile_slots
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.slack_reglage import COLONNE

revision = "20261119_1200_slack_mention_dflt"
down_revision = "20261118_1500_unipile_slots"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs ALTER COLUMN {COLONNE} SET DEFAULT TRUE")
    op.execute(f"UPDATE orgs SET {COLONNE} = TRUE WHERE NOT {COLONNE}")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs ALTER COLUMN {COLONNE} SET DEFAULT FALSE")
    op.execute(f"UPDATE orgs SET {COLONNE} = FALSE WHERE {COLONNE}")

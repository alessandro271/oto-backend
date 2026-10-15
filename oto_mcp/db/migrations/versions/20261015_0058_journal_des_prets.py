"""Le journal des prêts de compte : qui a prêté quel compte, à qui, quand, et le retrait.

Une table NEUVE, née entière — le DDL est le fragment
`db/schema/connectors.py::LOAN_EVENTS`, exécuté tel quel :
`connector_loan_events (id BIGSERIAL PK, at, actor_sub, org_id, entity_type, entity_id,
connector, account, target, action CHECK lend|revoke)`. Append-only, sans clé étrangère ;
deux index, posés sur la table vide : `actor_sub` (la fusion de comptes le repointe d'un
bloc) et la ligne du coffre (lecture par ligne, suivi au rechiffrement).

**Verrous.** `AccessExclusiveLock` sur la table neuve seule (vide, instantané) ; aucune
table existante n'est touchée, aucune n'est verrouillée. `lock_timeout` 5 s quand même :
un échec net, qu'on rejoue, plutôt qu'une attente.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment), y compris sur une base existante, avant de servir ; le
code d'avant l'ignore. Prod et préprod partagent la base : jouée une fois, elle sert les
deux. Une instance cible la reçoit à la montée (`migrer upgrade head`).

Retour arrière : retire la table (perd le journal des gestes faits depuis).

Révision : 0058_journal_des_prets
Précédente : 0057_contraintes_create_table
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.connectors import LOAN_EVENTS

revision = "0058_journal_des_prets"
down_revision = "0057_contraintes_create_table"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(LOAN_EVENTS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS connector_loan_events")

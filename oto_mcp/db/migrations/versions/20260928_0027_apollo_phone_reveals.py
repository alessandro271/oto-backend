"""apollo_phone_reveals : les téléphones révélés par Apollo, reçus par oto.

Table NEUVE et ses deux index — le fragment `db/schema/connectors.py::APOLLO_PHONE_REVEALS`,
exécuté tel quel. Une ligne par reveal commandé : l'empreinte du jeton de l'URL de
réception (`apollo_receiver.py`), l'org et le compte qui l'ont commandé, le `request_id`
d'Apollo, puis le corps qu'Apollo a POSTé. Rétention : trente jours (celle d'Apollo),
purgée par `oto-mcp maintenance apollo-phones`.

**Aucune clé étrangère** : ni verrou sur `orgs` à la création, ni sur `users` — des
lignes de trente jours n'en ont pas l'usage. Aucun `ALTER`, aucune reprise.

**Ordre** : le démarrage crée la même table s'il ne la trouve pas (`CREATE TABLE IF NOT
EXISTS`, ADR 0065) — révision et boot sont idempotents l'un envers l'autre. Le code du
lot ÉCRIT la table à chaque reveal de téléphone (la ligne naît avant l'appel à Apollo) :
la jouer **avant la fusion**, comme 0020, pour qu'un démarrage raté ne laisse pas la
préproduction refuser les reveals. L'ancien code ne la lit ni ne l'écrit.

⚠️ Prod et préprod partagent la MÊME base. Le retour arrière retire la table et les
numéros qu'elle garde ; le sondage d'Apollo reste alors le seul chemin de lecture, pour
les commandes de moins de trente jours. Tant que le fragment reste dans l'assemblage, le
démarrage suivant la recrée vide.

Révision : 0027_apollo_phone_reveals
Précédente : 0026_transcription_tours
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.connectors import APOLLO_PHONE_REVEALS

revision = "0027_apollo_phone_reveals"
down_revision = "0026_transcription_tours"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(APOLLO_PHONE_REVEALS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS apollo_phone_reveals")

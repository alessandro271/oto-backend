"""L'empreinte des facteurs de double authentification d'un compte.

Une table NEUVE, née entière : `empreintes_2fa` (`db/schema/users.py::EMPREINTES_2FA`) —
posée aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL.
Aucune FK. Écrite et lue par la réception du webhook Logto (`auth/revocation_2fa.py`) et
par la remise à zéro de la double authentification (`capabilities/users_admin.py`).

**Verrous.** Aucun sur une table existante.

**Ordre avec le code : avant le code.** Le code de ce lot lit la table à chaque
`User.Data.Updated` ; l'ancien code l'ignore.

Retour arrière : retire la table. Les empreintes se perdent ; la livraison suivante de
chaque compte en repose une sans fermer ses sessions.

Révision : 20261124_1200_empreintes_2fa
Précédente : 20261123_1433_embed_file
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.users import EMPREINTES_2FA

revision = "20261124_1200_empreintes_2fa"
down_revision = "20261123_1433_embed_file"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(EMPREINTES_2FA)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS empreintes_2fa")

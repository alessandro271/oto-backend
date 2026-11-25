"""L'instant avant lequel tout jeton d'un compte est refusé.

Une table NEUVE, née entière : `revocations_comptes` (`db/schema/users.py::REVOCATIONS_COMPTES`)
— posée aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL.
Aucune FK. Écrite par la fermeture de toutes les sessions d'un compte
(`auth/revocation_mot_de_passe.fermer_tout`), lue à chaque requête authentifiée par la
garde d'identité, dans la lecture qu'elle faisait déjà (`db.get_suspension`).

**Verrous.** Aucun sur une table existante.

**Ordre avec le code : avant le code.** Le code de ce lot lit la table à chaque requête
authentifiée ; l'ancien code l'ignore.

Retour arrière : retire la table. Les dates posées se perdent : un jeton émis avant une
fermeture redevient servi jusqu'à son terme (un JWT d'accès vit une heure ; les jetons
`oto_` du compte, eux, restent révoqués en base).

Révision : 20261125_1103_revocations
Précédente : 20261124_1200_empreintes_2fa
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.users import REVOCATIONS_COMPTES

revision = "20261125_1103_revocations"
down_revision = "20261124_1200_empreintes_2fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(REVOCATIONS_COMPTES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS revocations_comptes")

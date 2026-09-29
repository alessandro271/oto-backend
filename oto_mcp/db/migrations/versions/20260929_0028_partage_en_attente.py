"""partage_en_attente : une invitation peut porter UN objet au lieu d'une org.

Six colonnes additives sur `org_invitations` et un index unique partiel — le fragment
`db/schema/orgs.py::INVITATIONS_RESSOURCE` puis
`db/invitations_ressource.py::DDL_INDEX_RESSOURCE_ATTENTE`, exécutés tels quels (le
démarrage les joue aussi) : `resource_type`, `resource_kind`, `resource_id`,
`resource_role`, `resource_ttl_days`, `resource_name`, toutes NULL par défaut. Partager un objet avec
une adresse sans compte (`oto_resource op=share`) pose une ligne `org_id` NULL qui
les porte ; l'inscription ou l'acceptation du lien donne l'accès à CET objet, jamais
une adhésion (`oto_mcp/partage_en_attente.py`).

`ADD COLUMN` sans défaut : écriture de catalogue seule. L'index unique partiel
(`resource_kind IS NOT NULL`) ne porte aucune ligne existante ; la table compte
quelques centaines de lignes. Verrous bornés par `lock_timeout` : un échec net, qu'on
rejoue.

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit ni n'écrit ces
colonnes. Le code du lot les LIT à chaque acceptation d'invitation et à chaque
inscription (`reconcile_signup_with_invitation`) : à jouer **avant la fusion**, pour
qu'un démarrage raté ne laisse pas les invitations d'org en panne. Le retour arrière
retire l'index et les colonnes ; le code qui les lit doit être retiré avant lui, et
les partages encore en attente sont perdus (les personnes invitées n'ont pas de
compte : rien d'autre ne disparaît).

Révision : 0028_partage_en_attente
Précédente : 0027_apollo_phone_reveals
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.invitations_ressource import DDL_INDEX_RESSOURCE_ATTENTE
from oto_mcp.db.schema.orgs import INVITATIONS_RESSOURCE

revision = "0028_partage_en_attente"
down_revision = "0027_apollo_phone_reveals"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(INVITATIONS_RESSOURCE)
    op.execute(DDL_INDEX_RESSOURCE_ATTENTE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP INDEX IF EXISTS idx_org_invitations_ressource_attente")
    for col in ("resource_name", "resource_ttl_days", "resource_role", "resource_id",
                "resource_kind", "resource_type"):
        op.execute(f"ALTER TABLE org_invitations DROP COLUMN IF EXISTS {col}")

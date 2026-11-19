"""Partages : le rôle `contributor` (« Use » d'une app) entre dans `resource_grants`.

Une app se partage en quatre rôles : View (`viewer`), Use (`contributor`), Edit
(`editor`), Manage (`manager`). `contributor` ouvre l'app et clique ses boutons sans la
modifier ; chaque geste reste jugé aux droits du lecteur, bornés par l'auteur
(`capabilities/apps_appel.py`). Sa projection CONTENU est `read`.

Le CHECK `resource_grants_role_check` s'ÉLARGIT : retiré puis reposé sous le même nom,
`NOT VALID` puis validé hors transaction (toutes les lignes existantes y répondent déjà ;
la validation ne prend qu'un `ShareUpdateExclusiveLock`, qui laisse lire et écrire).
Une base neuve le reçoit tel quel du `CREATE TABLE` (`db/schema/grants.py`) ;
`contraintes_create_table.RESOURCE_GRANTS_ROLE` en suit la forme.

**Verrous.** `AccessExclusiveLock` bref sur `resource_grants` (catalogue seul) pour le
retrait et la pose `NOT VALID`. `lock_timeout` 5 s.

**Ordre avec le code : la révision d'abord.** Le code d'avant tourne sur le CHECK élargi
sans rien voir (il n'écrit jamais `contributor`). Le code neuf sur l'ancien CHECK
refuserait seulement le partage Use d'une app (violation de contrainte). La révision ne
promeut AUCUN partage : l'audit (`oto-mcp maintenance apps-lecteurs-actifs`, à blanc) dit
quels `viewer` cliquaient, et leur promotion (`--appliquer`) est une décision de
l'opérateur, après lecture, qui ne se joue qu'APRÈS cette révision.

⚠️ **Tant qu'un code d'avant démarre sur la base**, son rattrapage de rôle
(`db/_init.py`) ramène chaque `contributor` à `viewer` : il ne le connaît pas.

Retour arrière : les partages `contributor` redeviennent `viewer` (ils ouvrent l'app,
sans ses boutons), puis le CHECK retrouve sa forme d'avant.

Révision : 20261119_1300_app_use_role
Précédente : 20261119_1200_slack_mention_dflt
"""
from __future__ import annotations

from alembic import op

revision = "20261119_1300_app_use_role"
down_revision = "20261119_1200_slack_mention_dflt"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_NOM = "resource_grants_role_check"


def _poser(roles: tuple[str, ...]) -> None:
    liste = ", ".join(f"'{r}'" for r in roles)
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE resource_grants DROP CONSTRAINT IF EXISTS {_NOM}")
    op.execute(f"ALTER TABLE resource_grants ADD CONSTRAINT {_NOM} "
               f"CHECK (role IN ({liste})) NOT VALID")
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE resource_grants VALIDATE CONSTRAINT {_NOM}")


def upgrade() -> None:
    _poser(("viewer", "contributor", "editor", "manager"))


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("UPDATE resource_grants SET role = 'viewer', permission = 'read' "
               "WHERE role = 'contributor'")
    _poser(("viewer", "editor", "manager"))

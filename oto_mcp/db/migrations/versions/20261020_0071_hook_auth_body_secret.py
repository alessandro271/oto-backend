"""Le préréglage webhook `lemlist` s'appelle `body_secret` : les lignes stockées suivent (#1286).

`runner_triggers.hook_auth` porte le NOM du préréglage, et la garde SQL des livraisons
filtre par ce nom (`db.trigger_par_secret(…, hook_auth)`). Le code de ce lot ne connaît
plus `lemlist` — ni préréglage, ni alias, une pose qui le demande est refusée en le
nommant (`hook_auth_renamed`) : une ligne restée `lemlist` serait FERMÉE (404). Cette
révision réécrit donc `lemlist` en `body_secret`, même preuve sous un nom générique (le
porteur `otoh_` dans le champ `secret` du corps). Le porteur haché, l'adresse et le
reste de la ligne ne bougent pas : la source garde ce qu'elle détient.

**Verrous.** Un `UPDATE` des seules lignes `lemlist` de `runner_triggers` (quelques-unes) ;
`lock_timeout` 5 s : le tick lit la table en continu, un échec net qu'on rejoue plutôt
qu'une attente.

**Ordre avec le code : APRÈS le déploiement, ou juste avant — sans fenêtre utile entre
les deux.** L'ancien code ne connaît pas `body_secret` : une ligne réécrite avant le
déploiement reste fermée jusqu'à lui ; le nouveau ne connaît plus `lemlist` : une ligne
non réécrite après le déploiement reste fermée jusqu'à la révision. Prod et préprod
partagent la base : la jouer au tag de prod, aussitôt la bascule faite.

Retour arrière : l'inverse exact (`body_secret` → `lemlist`), pour l'ancien code qui ne
connaît que ce nom. Avant ce lot, aucune ligne ne pouvait porter `body_secret`.

Révision : 0071_hook_auth_body_secret
Précédente : 0070_triggers_outils_plateforme
"""
from __future__ import annotations

import logging

from alembic import context, op

revision = "0071_hook_auth_body_secret"
down_revision = "0070_triggers_outils_plateforme"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
#: Les deux noms, figés ici : une révision est de l'histoire, elle ne lit pas le code
#: du jour (`runner_hook.ANCIEN_NOM_BODY_SECRET`, `runner_hook.BODY_SECRET`).
_ANCIEN, _NEUF = "lemlist", "body_secret"
logger = logging.getLogger("alembic.runtime.migration")


def _renommer(de: str, vers: str) -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire : le compte se lit au
        # journal de la vraie montée.
        return
    op.execute(_ATTENTE_MAX)
    n = op.get_bind().exec_driver_sql(
        "UPDATE runner_triggers SET hook_auth = %(vers)s WHERE hook_auth = %(de)s",
        {"de": de, "vers": vers}).rowcount
    logger.info("0071 : runner_triggers.hook_auth — %d ligne(s) `%s` → `%s`", n, de, vers)


def upgrade() -> None:
    _renommer(_ANCIEN, _NEUF)


def downgrade() -> None:
    _renommer(_NEUF, _ANCIEN)

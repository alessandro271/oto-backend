"""Les listes d'outils enregistrées suivent le départ des personas de `lemlist_database`.

Le 23/10/2026, `lemlist_database` (la base partagée de lemlist) ne fait plus que lire, pour
qu'une recette puisse l'appeler : ses ops `persona_create` et `persona_delete` passent à
`lemlist_act(op="create_persona" / "delete_persona")`, l'acteur unique du connecteur. Cette
révision lit `outils_retires.OPS_DEPLACEES_PERSONAS`, la table de ce lot, sans la recopier.

**Le sens : masquer reste large** (décision du 21/10/2026 ; cf.
`20261021_1219_outils_act_msg`, dont la mécanique est reprise telle quelle, et
`20261021_1352_outils_act_envois`, dont c'est le même geste) :

- une liste qui MASQUE `lemlist_database` le garde masqué ET masque `lemlist_act` : tout ce
  que l'outil faisait reste caché ;
- une liste qui AUTORISE `lemlist_database` n'est pas touchée : il ne fait plus que lire ;
- une liste qui autorise `lemlist_act` le GARDE, même reçu de `lemlist_inbox_send` : il
  porte désormais aussi `create_persona` / `delete_persona`, et un agent qui l'avait les
  reçoit — choix assumé du 23/10/2026, aucun agent vivant ne perd l'envoi
  (`outils_retires.ops_en_plus` ne compte pas les ops de ce lot). Une liste qui le
  masque le garde masqué.

Le journal des appels et les travaux TERMINÉS ne sont pas réécrits.

**Ordre avec le code : APRÈS le déploiement, ou juste avant** (cf. 0062). Prod et préprod
partagent la base : la jouer au tag de prod.

Retour arrière : aucun — les ops d'écriture du lecteur sont ce que ce lot retire.

Révision : 20261023_0713_lemlist_personas
Précédente : 20261022_2112_forfait_fenetre
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import context, op

revision = "20261023_0713_lemlist_personas"
down_revision = "20261022_2112_forfait_fenetre"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def _revision_des_messageries():
    """La révision des messageries, pour sa mécanique (`correspondances`,
    `reecrire_les_listes`) — sa table à elle, `RENOMMAGES_MESSAGERIES`, n'est pas lue."""
    chemin = Path(__file__).with_name("20261021_1219_outils_act_msg.py")
    spec = importlib.util.spec_from_file_location("revision_outils_act_msg", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def correspondances() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """`(autorise, masque)` du lot des personas."""
    from oto_mcp import outils_retires
    return _revision_des_messageries().correspondances(
        outils_retires.OPS_DEPLACEES_PERSONAS)


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire (cf. 0062).
        return
    op.execute(_ATTENTE_MAX)
    _revision_des_messageries().reecrire_les_listes(op.get_bind(), *correspondances(),
                                                    revision)


def downgrade() -> None:
    raise NotImplementedError(
        f"{revision} n'a pas de retour arrière : les ops d'écriture du lecteur sont ce que "
        "ce lot retire (cf. docstring).")

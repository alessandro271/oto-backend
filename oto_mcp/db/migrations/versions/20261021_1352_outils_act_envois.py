"""Les listes d'outils enregistrées suivent le lot des envois : ce qui atteint un tiers
passe à `<ns>_act`.

Le 21/10/2026, après les messageries, les lecteurs qui envoyaient encore à un tiers :
LinkedIn (publication, invitations, actes sur un membre → `linkedin_unipile_act`), Apollo,
Lightfield, Luma, Pennylane, SignWell, Productlane, Ahrefs (→ `<ns>_act`), et l'outil
d'envoi de l'inbox lemlist (→ `lemlist_act`). Cette révision lit les deux tables de ce
lot dans `outils_retires`, sans les recopier :

- `RENOMMAGES_ENVOIS` — les outils d'envoi DÉDIÉS, retirés (`linkedin_unipile_publish`,
  `linkedin_unipile_invite`, `lemlist_inbox_send`) ;
- `OPS_DEPLACEES` — les lecteurs MIXTES, qui RESTENT servis sous leur nom et perdent
  leurs ops d'envoi (`apollo_email`, `linkedin_unipile_post`…).

**Le sens, décision d'Alexis du 21/10/2026 : autoriser reste étroit, masquer reste large**
(cf. `20261021_1219_outils_act_msg`, dont la mécanique est reprise telle quelle).

- Une liste qui AUTORISE un outil d'envoi dédié reçoit `<ns>_act` à sa place SEULEMENT si
  `<ns>_act` ne fait rien de plus que lui (`lemlist_inbox_send` → `lemlist_act`) ; sinon
  elle ne reçoit RIEN (décision du 21/10/2026) : `linkedin_unipile_publish` et
  `linkedin_unipile_invite` autorisés disparaissent, car `linkedin_unipile_act` publie,
  invite ET envoie des messages. Le refus de l'ancien nom dit d'ajouter l'acteur
  explicitement. Une liste qui autorise un lecteur mixte n'est pas touchée : il ne fait
  plus que lire.
- Un acteur du lot des messageries que ce lot ÉLARGIT (`linkedin_unipile_act`, qui ne
  servait que la messagerie) : une liste qui l'autorise le perd — elle l'avait reçu, ou
  donné, pour moins que ce qu'il fait désormais. Sans effet si les deux révisions se
  jouent ensemble (celle des messageries ne le donne plus) ; c'est la garde du cas où
  elle aurait été jouée seule avant.
- Une liste qui MASQUE reçoit tout ce que l'outil faisait : l'outil d'envoi dédié masqué
  masque `<ns>_act` ; le lecteur mixte masqué reste masqué ET masque `<ns>_act`.

Le journal des appels et les travaux TERMINÉS ne sont pas réécrits.

**Ordre avec le code : APRÈS le déploiement, ou juste avant** (cf. 0062). Prod et préprod
partagent la base : la jouer au tag de prod.

Retour arrière : aucun — les anciens noms et les ops d'envoi des lecteurs sont la faille
corrigée.

Révision : 20261021_1352_outils_act_envois
Précédente : 20261021_1219_outils_act_msg
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import context, op

revision = "20261021_1352_outils_act_envois"
down_revision = "20261021_1219_outils_act_msg"
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


def acteurs_elargis() -> tuple[str, ...]:
    """Les acteurs nés au lot des messageries auxquels ce lot ajoute des ops."""
    from oto_mcp import outils_retires
    lot = outils_retires.RENOMMAGES_ENVOIS + outils_retires.OPS_DEPLACEES
    return tuple(sorted({r.outil for r in outils_retires.RENOMMAGES_MESSAGERIES
                         if r.outil.endswith("_act")} & {r.outil for r in lot}))


def correspondances() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """`(autorise, masque)` du lot des envois."""
    from oto_mcp import outils_retires
    autorise, masque = _revision_des_messageries().correspondances(
        outils_retires.RENOMMAGES_ENVOIS + outils_retires.OPS_DEPLACEES)
    for acteur in acteurs_elargis():
        autorise[acteur] = ()
        masque[acteur] = (acteur,)
    return autorise, masque


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire (cf. 0062).
        return
    op.execute(_ATTENTE_MAX)
    _revision_des_messageries().reecrire_les_listes(op.get_bind(), *correspondances(),
                                                    revision)


def downgrade() -> None:
    raise NotImplementedError(
        f"{revision} n'a pas de retour arrière : les anciens noms et les ops d'envoi des "
        "lecteurs sont la faille corrigée (cf. docstring).")

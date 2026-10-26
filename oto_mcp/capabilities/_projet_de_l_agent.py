"""Le projet où travaille un agent : un de ceux de sa procédure (tranché le 24/10/2026).

Une procédure peut être rangée dans plusieurs projets de l'org ; un agent, lui, travaille
dans UN seul — chaque run se fait dans un projet, qui décide des tableaux que ses slots
désignent et des comptes qu'il utilise. Pour qu'une procédure travaille pour deux
projets, on lui donne un agent dans chacun (`runner_triggers._projet_de_la_pose`).

Un agent qui ne porte pas de projet à lui (posé par MCP sans `project_id`, ou d'avant la
règle) travaille dans le premier projet de sa procédure QUE L'IDENTITÉ DU RUN LIT
(`lisible`) : c'est ce que son run reçoit (`de_l_agent`) et ce que sa lecture sert
(`servis`), pour que l'écran et le run disent la même chose. Un projet que cette identité
ne lit pas ne lui est jamais attribué : le run part alors sans projet, et son instruction
le lui dit (`_instruction.sans_projet_lisible`) — jamais en silence.

⚠️ DÉDUIT, pas recopié sur l'agent : la plupart des agents tournent sur le forfait de
leur propriétaire, que lui seul (et qui il nomme) peut modifier — une copie écrite au
rangement de la procédure sauterait précisément ceux-là. Le projet stocké sur l'agent,
quand il en a un, reste lu d'abord : il a passé le contrôle à la pose
(`runner_triggers._exiger_projet_de_l_org`), et l'appel du run le re-garde.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .. import db, ownership

log = logging.getLogger(__name__)


def lisible(sub: Optional[str], org_id: int, project_id: int) -> bool:
    """Le projet existe, `sub` le lit, et il est visible dans l'org `org_id` — LE contrôle
    d'un projet d'agent, le même pour le projet demandé à la pose
    (`runner_triggers._exiger_projet_de_l_org`) et pour celui attribué par défaut."""
    if not sub:
        return False
    pid = str(int(project_id))
    return (db.get_project_by_id(int(pid)) is not None
            and ownership.can_access(sub, "project", pid, "read")
            and ownership.visible_in_org(sub, org_id, "project", pid))


def premier_lisible(sub: Optional[str], org_id: int, projets: list[int]) -> Optional[int]:
    """Le premier des `projets` (dans leur ordre) que `sub` lit, None s'il n'en lit aucun.
    S'arrête au premier : un projet lisible ne coûte que sa propre vérification."""
    return next((int(p) for p in projets if lisible(sub, org_id, int(p))), None)


@dataclass(frozen=True)
class ProjetDuRun:
    """Le projet d'un run, et pourquoi il n'en a pas quand sa procédure en a."""
    project_id: Optional[int]
    #: La procédure est rangée, mais dans des projets que l'identité du run ne lit pas :
    #: le run part sans projet, et son instruction le dit.
    illisible: bool = False


def de_l_agent(t: dict, sub: Optional[str] = None) -> ProjetDuRun:
    """Le projet de la charge d'un run de `t` : le sien, sinon le premier de sa procédure
    que lit `sub` — l'identité du run (défaut : le propriétaire de l'agent).

    Une lecture qui échoue LÈVE : partir sans projet ferait échouer les slots du run sans
    que rien ne dise pourquoi."""
    if t.get("project_id") is not None:
        return ProjetDuRun(int(t["project_id"]))
    if not t.get("org_id") or not t.get("procedure"):
        return ProjetDuRun(None)
    org_id, slug = int(t["org_id"]), t["procedure"]
    projets = db.projets_des_procedures(org_id, [slug]).get(slug, [])
    if not projets:
        return ProjetDuRun(None)
    qui = sub or t.get("sub")
    projet = premier_lisible(qui, org_id, projets)
    if projet is None:
        log.warning("déclencheur %s : la procédure `%s` n'est rangée que dans des projets "
                    "que %s ne lit pas (%s) — le run part sans projet, et son instruction "
                    "le dit", t.get("id"), slug, qui, projets)
        return ProjetDuRun(None, illisible=True)
    return ProjetDuRun(projet)


def servis(org_id: int, triggers: list[dict]) -> list[dict]:
    """Les agents tels que servis : un agent sans projet à lui porte le premier de sa
    procédure que lit son propriétaire — la même réponse que reçoit son run d'horloge.
    Une requête pour les projets de toute la liste ; chaque couple (propriétaire,
    projet) ne se vérifie qu'une fois."""
    sans = [t["procedure"] for t in triggers
            if t.get("project_id") is None and t.get("procedure")]
    if not sans:
        return triggers
    projets = db.projets_des_procedures(int(org_id), sans)
    vus: dict[tuple, bool] = {}

    def _lu(sub: Optional[str], pid: int) -> bool:
        if (sub, pid) not in vus:
            vus[(sub, pid)] = lisible(sub, int(org_id), pid)
        return vus[(sub, pid)]

    out = []
    for t in triggers:
        if t.get("project_id") is None and t.get("procedure") in projets:
            projet = next((p for p in projets[t["procedure"]] if _lu(t.get("sub"), p)), None)
            if projet is not None:
                t = {**t, "project_id": projet}
        out.append(t)
    return out

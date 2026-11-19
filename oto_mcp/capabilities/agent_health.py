"""La SANTÉ des agents d'une org — l'onglet Santé d'un tableau de bord, en LECTURE.

`GET /api/orgs/{id}/agent-health` — membres d'une org vivante (`ORG_MEMBER_OF_LIVE`) : les
agents allumés qu'ils voient, ceux qui fonctionnent, ce qui attend le CLIENT (un problème
par cause, avec les agents qu'il arrête), ce qui est à nous, et ce que le médecin a réparé
ces trente derniers jours. Rien ne s'y écrit.

Une lecture par ORG, jamais une par agent : l'onglet d'une org de deux cents agents
coûte quelques requêtes. Un problème se dit avec le texte que reçoit Slack
(`alertes_slack_agents.message_du_probleme`) et se regroupe : les agents dont le client
lirait exactement le même problème (titre, geste, lien) forment UN problème.

⚠️ **Deux états, comme la carte d'un agent** (`agent-state.ts` du tableau de bord) : un
agent fonctionne, ou il ne fonctionne pas. Ne fonctionne pas : soit il attend le CLIENT
(`needs_you`, côté titulaire : le geste), soit c'est à NOUS (`on_us` : côté plateforme,
ou pas encore compris — une phrase qui dit qu'on s'en occupe, aucun geste). Un membre ne
voit que les agents qu'il voit dans la liste des agents (`_acces_agent.niveaux`) ; un
admin les voit tous.

Sous le réglage d'instance des alertes (`alertes_slack_agents.actives`), comme Réglages ▸
Alertes et `problem` sur la file : éteint, 404 `agent_alerts_off`. Une org d'un tenant
tiers n'est pas suivie par le médecin (`covered: false`) : rien ne s'y dit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel

from ._authz import ORG_MEMBER_OF_LIVE
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_ID = {"id": "org_id"}

#: Ce qui se dit d'une panne pas encore comprise (`cote = inconnu`), par langue : aucun
#: geste.
A_NOUS_INCONNU = {
    "fr": "Nous cherchons pourquoi il s'est arrêté. Nous vous dirons s'il y a quelque "
          "chose à faire.",
    "en": "We are looking into why it stopped. We will tell you if there is anything "
          "to do.",
}

#: Une panne de NOTRE côté ne se dit pas avant six heures — la carte de l'agent n'en dit
#: rien non plus, la boucle la relance. Passé six heures, une ligne par côté, dans
#: l'onglet seulement. La durée se lit sur l'épisode (`ouvert_at`), pas sur l'incident de
#: la boucle, qui vit en mémoire.
DUREE_LONGUE = timedelta(hours=6)
_A_NOUS_LONGUE = {
    "fr": "Un problème de notre côté l'arrête depuis plus de six heures. Nous y travaillons : "
          "rien à faire du vôtre.",
    "en": "A problem on our side has stopped it for more than six hours. We are working on "
          "it: nothing to do on yours.",
}

#: Ce qui se dit d'une relance qui a guéri l'agent, par langue.
_RELANCE = {
    "fr": "Il s'est arrêté sur une panne passagère de notre côté. Nous l'avons relancé.",
    "en": "It stopped on a short outage on our side. We ran it again.",
}


class AgentHealthInput(BaseModel):
    org_id: int


class AgentHealth(BaseModel):
    """`watched` agents allumés que l'appelant voit, dont `working` fonctionnent ;
    `needs_you` = un problème par cause qui attend le client : `{headline, todo, action,
    link, since, agents: [{id, label}], connectors, error}` ; `on_us` = ce qui est à
    nous, par côté : `{side: platform|operator, headline, since, lasting, agents,
    connectors, error}` — `connectors` = les connecteurs que nomment les motifs de ses
    agents (libellés de produit, sans doublon, liste vide sinon), `error` = la première
    réponse de fournisseur lue, passée par la liste blanche (texte ou null), jamais notre
    texte interne ; une panne de la plateforme ne s'y dit qu'ouverte depuis six heures au
    moins (`lasting`) ; avant, son agent compte comme fonctionnant, comme sur sa carte ;
    `fixed` = ce que le médecin a réparé ces trente derniers jours, le plus récent
    d'abord : `{episode_id, agent: {id, label}, at, what}` ; `covered` = le médecin suit
    cette org (false : org d'un tenant tiers, rien ne s'y dit)."""
    watched: int
    working: int
    needs_you: list[dict]
    on_us: list[dict]
    fixed: list[dict]
    covered: bool


def _visibles(ctx: ResolvedCtx, org_id: int) -> dict[int, dict]:
    """Les agents que l'appelant voit — la règle de la liste des agents
    (`_acces_agent.niveaux`) : une lecture de partages pour toute l'org."""
    from .. import db
    from . import _acces_agent
    agents = db.list_triggers(org_id)
    niveaux = _acces_agent.niveaux(ctx.sub, org_id, agents)
    return {int(t["id"]): t for t in agents if niveaux[int(t["id"])]}


def _nom(t: dict) -> str:
    return (t.get("label") or "").strip() or t.get("procedure") or f"#{t['id']}"


def _quand(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v else None


def _date(v: Any) -> datetime:
    """Une date d'épisode, objet ou texte ISO (les lignes passent par `_str_dict_row`)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def details(p: dict, ep: dict) -> None:
    """Ajoute à l'item ce que le motif de l'épisode dit au client : le connecteur
    (`causes_pannes.connecteur_de_la_panne`, modèle de l'agent en repli) et la réponse du
    fournisseur (`causes_pannes.erreur_du_fournisseur`) — jamais notre texte interne."""
    from .. import causes_pannes
    connecteur = causes_pannes.connecteur_de_la_panne(ep.get("motif"), ep.get("modele"))
    if connecteur and connecteur not in p["connectors"]:
        p["connectors"].append(connecteur)
    p["error"] = p["error"] or causes_pannes.erreur_du_fournisseur(ep.get("motif"))


def _lire(ctx: ResolvedCtx, inp: AgentHealthInput) -> dict:
    from .. import alertes_slack_agents as A
    from ..alerte_agents_bloques import org_a_nous
    from ..db import sante_agents
    from .agent_alerts import _exiger_les_alertes
    _exiger_les_alertes()
    org_id = int(inp.org_id)
    visibles = _visibles(ctx, org_id)
    allumes = {i: t for i, t in visibles.items() if t.get("enabled")}
    langue = A.langue(org_id)
    marque = A.marque_de_l_org(org_id).nom

    problemes: dict[tuple, dict] = {}
    a_nous: dict[str, dict] = {}
    maintenant = datetime.now(timezone.utc)
    for ep in sante_agents.episodes_vivants(org_id):
        t = allumes.get(int(ep["trigger_id"]))
        if t is None:
            continue
        if ep["cote"] == "plateforme" and maintenant - _date(ep["ouvert_at"]) < DUREE_LONGUE:
            # Une panne de NOTRE côté se tait six heures, comme sur la carte de l'agent
            # (`agent-state.ts` : rien de dit) — la boucle la relance ; l'agent compte
            # comme fonctionnant.
            continue
        if ep["cote"] != "proprietaire":
            cote = "plateforme" if ep["cote"] == "plateforme" else "inconnu"
            phrases = _A_NOUS_LONGUE if cote == "plateforme" else A_NOUS_INCONNU
            p = a_nous.setdefault(cote, {
                "side": "platform" if cote == "plateforme" else "operator",
                "headline": phrases.get(langue, phrases["en"]),
                "since": ep["ouvert_at"], "agents": [], "connectors": [], "error": None})
            p["since"] = min(p["since"], ep["ouvert_at"])
            p["agents"].append({"id": int(t["id"]), "label": _nom(t)})
            details(p, ep)
            continue
        pb = A.message_du_probleme(org_id, ep, langue, marque=marque, sub=ctx.sub)
        cle = (pb["headline"], pb.get("todo"), pb.get("link"), pb.get("action"))
        p = problemes.setdefault(cle, {**pb, "since": ep["ouvert_at"], "agents": [],
                                       "connectors": [], "error": None})
        p["since"] = min(p["since"], ep["ouvert_at"])
        p["agents"].append({"id": int(t["id"]), "label": _nom(t)})
        details(p, ep)
    arretes = {a["id"] for p in [*problemes.values(), *a_nous.values()]
               for a in p["agents"]}

    fixed = []
    for ep in sante_agents.reparations_recentes(org_id):
        t = visibles.get(int(ep["trigger_id"]))
        if t is None:
            continue
        note = ep.get("repair_note") or {}
        fixed.append({"episode_id": int(ep["id"]),
                      "agent": {"id": int(t["id"]), "label": _nom(t)},
                      "at": _quand(ep["clos_at"]),
                      "what": note.get("what") or _RELANCE.get(langue, _RELANCE["en"])})

    needs_you = sorted(problemes.values(), key=lambda p: p["since"])
    on_us = sorted(a_nous.values(), key=lambda p: p["since"])
    for p in on_us:
        # Une panne de la plateforme n'arrive ici qu'après six heures (plus haut).
        p["lasting"] = p["side"] == "platform"
    for p in [*needs_you, *on_us]:
        p["since"] = _quand(p["since"])
    return {"watched": len(allumes), "working": len(set(allumes) - arretes),
            "needs_you": needs_you, "on_us": on_us, "fixed": fixed,
            "covered": org_a_nous(org_id, qui="santé des agents")}


CAPABILITIES += [
    Capability(key="org.agent_health.get", handler=_lire, Input=AgentHealthInput,
               Output=AgentHealth, authz=ORG_MEMBER_OF_LIVE("org_id"), mcp=None,
               rest=RestBinding("GET", "/api/orgs/{id}/agent-health", _ID)),
]

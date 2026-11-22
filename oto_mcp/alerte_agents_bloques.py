"""Prévenir l'admin d'une org quand un de ses agents programmés est BLOQUÉ (signal 1168).

Une routine a buté 33 passages de suite sur une clé refusée par le fournisseur, et
personne n'a été prévenu : chaque passage écrivait son compte rendu dans un canal que
plus personne ne lisait. Un compte rendu dans le canal que l'agent tient n'est pas une
escalade. Ce module en est une, et la plus petite possible :

- **qui** : dans l'org PROPRIÉTAIRE de l'agent (`runner_triggers.org_id`), son titulaire
  encore membre et les admins clients (`destinataires`) ; un admin reçoit tous les
  agents de l'org, un membre les seuls qu'il a le droit de voir (`agents_visibles`). Par
  le courrier de PLATEFORME, sous la marque de l'org (`org_store.org_front`) — jamais
  par un connecteur de l'org, qui peut être précisément ce qui est tombé. L'équipe de la
  plateforme reçoit un courriel SÉPARÉ, qui nomme l'org (`alertes_equipe`) ;
- **quand** : un épisode — les travaux morts consécutifs depuis le dernier succès — qui
  atteint `SEUIL` (déclaré ici), annoncé UNE fois (`db/alertes_agents.py`), hors des
  heures calmes par défaut du courrier (`scheduler.DEFAULT_QUIET_HOURS`) : un épisode
  vu la nuit part au premier passage du matin ;
- **un courriel par destinataire et par org**, quel que soit le nombre d'agents bloqués ;
- **où** : seulement la PRODUCTION — la boucle se déclare `tiers=True` dans
  `boucles_de_fond.py`, la préprod (même base) et le développement ne la composent pas ;
- ⚠️ **un agent dont la panne est la NÔTRE est écarté** (`medecin_agents.py`) : un
  épisode du côté plateforme ne s'annonce pas au titulaire, qui n'y peut rien — tant
  que la boucle le VOIT (`medecin_agents.FRAICHEUR`) : boucle éteinte, l'alerte reprend ;
- ⚠️ **une org d'un tenant tiers est ÉCARTÉE** (`db.org_tenant_slug`, fermé sur
  erreur) : on ne s'adresse pas aux clients d'un partenaire dans son produit sans une
  règle que le tenant aurait déclarée — il n'en existe aucune pour ce courrier
  (`docs/tenants.md`, convention du 08/10/2026 : rien de neuf par tenant). L'écart est
  compté et journalisé, jamais silencieux.

**Ce que dit le courriel** : sous le réglage d'instance des alertes
(`alertes_slack_agents.actives`), il parle comme Slack. Le texte de chaque agent est celui
du problème de son épisode vivant (`alertes_slack_agents.message_du_probleme` : la copie de
la cause approuvée, sinon le texte du médecin, sinon la phrase de la classe), et les agents
dont le client lirait exactement le même problème forment UN bloc qui les nomme, avec le
geste une fois et son bouton (`links`, comme Slack). ⚠️ Le motif BRUT (`motif` : un nom
d'outil, un code, un identifiant) n'y entre jamais : sans épisode lisible, c'est la phrase
par défaut. Le type de problème d'un agent se décide dans `type_de_probleme` (un échec,
une attente d'abonnement), les destinataires et ce que chacun voit dans
`destinataires_de_l_alerte` ; la ligne de fin dit à quel titre chacun le reçoit.
Réglage éteint, le courriel d'avant, mot pour mot.

⚠️ La marque vient APRÈS l'envoi : marquer d'abord ferait d'un envoi raté un silence
définitif — la panne même que ce module supprime.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Iterable, NamedTuple, Optional

log = logging.getLogger("oto_mcp.alerte_agents_bloques")

#: Le seuil DÉCLARÉ : trois travaux morts de suite. Un échec isolé se rejoue (le travail
#: retente déjà, `max_attempts`) ; deux peuvent être un amont instable une nuit ; trois
#: passages d'un agent quotidien, c'est trois jours de travail perdu.
SEUIL = 3

#: La cadence de la boucle. L'épisode s'annonce au plus tard un quart d'heure après le
#: travail qui l'ouvre (hors heures calmes).
INTERVALLE_S = 900

#: Les orgs d'un tenant tiers déjà dites au journal par CE processus : on le dit une fois,
#: pas toutes les quinze minutes.
_ECARTEES_DITES: set[int] = set()


class Destinataire(NamedTuple):
    """Une personne de l'org à prévenir. `admin` : un admin de l'org (ou le premier
    admin gardé faute de client) reçoit TOUT ; un membre ne reçoit que ce qu'il a le
    droit de voir (`agents_visibles`)."""
    sub: str
    adresse: str
    admin: bool


def destinataires(org_id: int, *, proprietaires: Iterable[str] = ()
                  ) -> list[Destinataire]:
    """De qui prévenir dans une org, dans l'ordre : les `proprietaires` (ceux dont les
    agents sont touchés) encore membres de l'org, puis TOUS ses admins — chacun une fois.

    ⚠️ **Le personnel de la plateforme est écarté** (`access.is_operator_role`), comme un
    compte suspendu : un opérateur est admin de nombreuses orgs clientes pour les aider,
    et c'est le CLIENT qui peut réparer ; l'équipe reçoit son propre courriel, séparé
    (`alertes_equipe`). Une org où ne restent que des opérateurs (une org de
    démonstration, une org interne) garde la règle d'avant : son premier admin
    joignable, pour que le message parte quelque part plutôt que nulle part.

    Ce que chacun reçoit n'est pas décidé ici : un admin reçoit tout, un membre ce qu'il
    a le droit de voir (`agents_visibles`, la règle d'accès des agents).

    Une seule source pour « qui prévient-on dans une org » : l'alerte d'agents bloqués,
    l'alerte de clé (`maintenance.alertes_credential`) et la langue de l'espace
    (`alertes_slack_agents.langue`, par `destinataire_admin`)."""
    from . import access, org_store
    from .db import users as db_users

    membres = org_store.list_org_members(org_id)
    admins = [m["sub"] for m in membres if m.get("org_role") == "org_admin"]
    du_lieu = {m["sub"] for m in membres}
    ordre = list(dict.fromkeys(
        [s for s in proprietaires or () if s and s in du_lieu] + admins))
    adresses = db_users.emails_by_subs(ordre)
    clients: list[Destinataire] = []
    for sub in ordre:
        if not adresses.get(sub):
            continue
        fiche = db_users.get_user(sub) or {}
        if fiche.get("suspended_at") or access.is_operator_role(sub, fiche.get("role")):
            continue
        clients.append(Destinataire(sub, adresses[sub], sub in admins))
    if clients:
        return clients
    repli = next((s for s in admins if adresses.get(s)), None)
    return [Destinataire(repli, adresses[repli], True)] if repli else []


def destinataire_admin(org_id: int) -> Optional[tuple[str, str]]:
    """`(sub, adresse)` du premier destinataire de l'org (`destinataires`), ou None —
    un admin CLIENT d'abord, un opérateur seulement s'il n'y a personne d'autre."""
    tous = destinataires(org_id)
    return (tous[0].sub, tous[0].adresse) if tous else None


def agents_visibles(sub: str, org_id: int, agents: list[dict]) -> list[dict]:
    """Ceux des `agents` (lignes qui portent `trigger_id` et `proprietaire`) que `sub`
    a le droit de voir — la règle d'accès des agents (`_acces_agent.niveaux` : son
    propriétaire, un admin de l'org, un partage en lecture ou en écriture)."""
    from .capabilities import _acces_agent
    if not agents:
        return []
    niveaux = _acces_agent.niveaux(sub, int(org_id), [
        {"id": int(a["trigger_id"]), "sub": a.get("proprietaire")} for a in agents])
    return [a for a in agents if niveaux.get(int(a["trigger_id"]))]


def destinataires_de_l_alerte(org_id: int, agents: list[dict], *,
                              equipe: Iterable[str] = ()) -> list[dict]:
    """Qui reçoit l'alerte d'une org, et ce que chacun y voit — le SEUL endroit qui le
    décide : `[{sub, adresse, agents, qualite}]`, un courriel chacun.

    - les personnes de l'org (`destinataires` : le titulaire encore membre, les admins
      clients, ou le premier admin faute de client) : un admin reçoit TOUS les agents
      (`qualite = admin`), un membre les seuls qu'il a le droit de voir
      (`agents_visibles`, `qualite = membre`) — rien à lui montrer, rien ne lui part ;
    - l'équipe de la plateforme (`equipe`, les adresses de `alertes_equipe`) : tous les
      agents, `qualite = equipe`, `sub` None — un courriel séparé, qui nomme l'org.

    `qualite` dit à quel titre le courriel est reçu (`_PAR_PROBLEME[…]["fin"]`). Liste
    sans client : personne à prévenir dans l'org."""
    out: list[dict] = []
    for d in destinataires(org_id, proprietaires=[a.get("proprietaire") for a in agents]):
        siens = list(agents) if d.admin else agents_visibles(d.sub, org_id, agents)
        out.append({"sub": d.sub, "adresse": d.adresse, "agents": siens,
                    "qualite": "admin" if d.admin else "membre"})
    if not out:
        return []
    return out + [{"sub": None, "adresse": a, "agents": list(agents), "qualite": "equipe"}
                  for a in equipe]


def type_de_probleme(agent: dict) -> str:
    """Le TYPE de problème d'un agent bloqué, le SEUL endroit qui le décide : `forfait`
    = ses travaux attendent un abonnement personnel à reconnecter (une ligne de
    `alertes_agents.agents_sans_forfait`) ; `echec` = des travaux morts de suite (une
    ligne de `alertes_agents.agents_bloques`). Un type s'ajoute ici, avec sa phrase
    (`probleme_de_l_agent`) et ses libellés (`_PAR_PROBLEME`)."""
    return "forfait" if agent.get("forfait") else "echec"


#: La phrase d'un agent qui attend un abonnement à reconnecter, par langue : il n'échoue
#: pas, il ne part plus ; seule la personne qui paie l'abonnement peut le reconnecter.
_FORFAIT = {
    "fr": ("Il attend que son {nom} soit reconnecté.",
           "La personne à qui il appartient le reconnecte dans Connecteurs, ou passe "
           "l'agent sur un autre modèle."),
    "en": ("It is waiting for its {nom} to be reconnected.",
           "Its owner reconnects it in Connectors, or switches the agent to another "
           "model."),
}


def org_a_nous(org_id: int, *, qui: str) -> bool:
    """L'org relève-t-elle du tenant primaire, sur réponse franche ? Fermé sur erreur.
    `qui` = le dispositif qui demande, nommé au journal. Partagé avec la boucle
    `medecin_agents`, qui écarte les mêmes orgs : une seule règle pour « cette org
    est-elle la nôtre »."""
    from . import db, tenancy
    try:
        return db.org_tenant_slug(int(org_id)) == tenancy.primary_slug()
    # noqa: SILENT — fermé par défaut, et dit : l'org est écartée et comptée.
    except Exception:  # noqa: BLE001
        log.warning("%s : tenant de l'org %s illisible — org écartée", qui, org_id,
                    exc_info=True)
        return False


def _heures_calmes(maintenant: datetime) -> bool:
    from . import scheduler
    return scheduler.compute_scheduled_at(maintenant, None, None, False) is not None


def probleme_de_l_agent(org_id: int, agent: dict, ep: Optional[dict], langue_: str, *,
                        marque: str, sub: Optional[str]) -> dict:
    """`{type, headline, todo, action, link}` : LE problème d'un agent bloqué, seul
    endroit où le courriel décide de quel problème il parle. `agent` = la ligne de
    `alertes_agents.agents_bloques`, `ep` = son épisode vivant, ou None.

    Avec un épisode : pas encore compris, et le médecin n'a rien écrit, on ne dit pas au
    client que c'est à lui (la phrase d'une classe le ferait) — comme l'onglet Santé ;
    sinon, le texte de l'épisode (`alertes_slack_agents.message_du_probleme`). Sans
    épisode : la phrase propre au TYPE (`type_de_probleme`) — la phrase par défaut d'un
    échec pour `echec` ; un type sans phrase lève. Un agent qui attend un abonnement
    (`forfait`) reçoit toujours la phrase de son type, avec le geste de reconnexion."""
    from . import alertes_slack_agents as A
    from . import links, runner_models
    from .capabilities.agent_health import A_NOUS_INCONNU
    type_ = type_de_probleme(agent)
    if type_ == "forfait":
        # L'épisode d'échec ne dit rien d'une attente : la phrase et le geste du type.
        titre, geste = _FORFAIT[langue_]
        nom = runner_models.libelle_abonnement(agent["famille"], en=langue_ == "en")
        return {"type": type_, "headline": titre.format(nom=nom), "todo": geste,
                "action": "connectors",
                "link": links.link_for("connectors", sub=sub, org=int(org_id))}
    if ep is None:
        if type_ != "echec":
            raise ValueError(f"type de problème sans phrase : {type_}")
        ep = {"classe": None, "cote": "proprietaire"}
    if ep.get("cote") == "inconnu" and not (ep.get("message_client") or {}).get("headline"):
        return {"type": type_, "headline": A_NOUS_INCONNU.get(langue_, A_NOUS_INCONNU["en"]),
                "todo": None, "action": None, "link": None}
    return {"type": type_,
            **A.message_du_probleme(org_id, ep, langue_, marque=marque, sub=sub)}


def problemes(org_id: int, agents: list[dict], langue_: str, *, marque: str,
              sub: Optional[str]) -> list[dict]:
    """Les agents bloqués d'une org rangés par PROBLÈME, comme Slack (`grouper`) :
    `[{type, headline, todo, action, link, since, runs, connectors, error, agents: [{id,
    label, url}]}]`, dans l'ordre du premier agent de chaque problème. Le texte vient de l'épisode vivant de l'agent
    (`probleme_de_l_agent`) ; un agent sans épisode lisible reçoit la phrase par défaut
    — jamais son motif brut. `sub` = le destinataire : les liens sont ceux de SON tableau
    de bord."""
    from . import links
    from .capabilities.agent_health import details
    from .db import sante_agents
    try:
        vivants = {int(e["trigger_id"]): e for e in sante_agents.episodes_vivants(org_id)}
    except Exception:  # noqa: BLE001 — ouvert : le courriel part, sur la phrase par défaut
        log.warning("alerte agents bloqués : épisodes de l'org %s illisibles — phrase par "
                    "défaut", org_id, exc_info=True)
        vivants = {}
    out: dict[tuple, dict] = {}
    for a in agents:
        tid = int(a["trigger_id"])
        ep = vivants.get(tid)
        pb = probleme_de_l_agent(org_id, a, ep, langue_, marque=marque, sub=sub)
        cle = (pb["type"], pb["headline"], pb.get("todo"), pb.get("link"), pb.get("action"))
        p = out.setdefault(cle, {**pb, "agents": [], "since": None, "runs": 0,
                                 "connectors": [], "error": None})
        depuis = str(a.get("depuis") or "")[:16] or None
        if depuis and (p["since"] is None or depuis < p["since"]):
            p["since"] = depuis
        p["runs"] = max(p["runs"], int(a.get("travaux") or 0))
        # Le connecteur et la réponse du fournisseur (liste blanche), comme l'onglet
        # Santé — lus, jamais recopiés : le motif brut ne sort pas.
        details(p, {"motif": a.get("motif") or (ep or {}).get("motif"),
                    "modele": a.get("modele") or (ep or {}).get("modele")})
        p["agents"].append({"id": tid, "label": a.get("libelle") or f"#{tid}",
                            "url": links.link_for("agent", sub=sub, org=int(org_id),
                                                  id=tid)})
    return list(out.values())


#: Le courriel qui parle comme Slack, par langue (`problemes`) : un titre qui dit ce qui
#: s'est arrêté, puis par problème le texte, le geste, et une petite table — quels
#: agents, quel connecteur, depuis quand et combien de passages, ce que le fournisseur a
#: répondu. Par TYPE de problème (`type_de_probleme`) : depuis quand, combien de
#: passages. Par QUALITÉ du destinataire (`destinataires_de_l_alerte` : admin, membre,
#: équipe) : la ligne de fin, qui dit à quel titre il le reçoit.
_PAR_PROBLEME = {
    "fr": {"un": "{agent} est arrêté", "n": "{n} agents sont arrêtés",
           "faire": "Le faire maintenant", "agent": "Agent", "agents": "Agents",
           "connecteur": "Connecteur", "connecteurs": "Connecteurs",
           "depuis": {"echec": "En échec depuis", "forfait": "Ne tourne plus depuis"},
           "passages": {"echec": "{n} passages en échec",
                        "forfait": "{n} passage(s) manqué(s)"},
           "erreur": "Erreur",
           "fin": {"admin": "Vous recevez cet email en tant qu'administrateur de cet "
                            "espace. C'est le seul à ce sujet. Coupez un agent dans Agents "
                            "si vous n'en avez plus besoin.",
                   "membre": "Vous recevez cet email parce que vous avez accès à ces "
                             "agents. C'est le seul à ce sujet. Coupez un agent dans "
                             "Agents si vous n'en avez plus besoin.",
                   "equipe": "Copie pour l'équipe de la plateforme : les personnes de "
                             "l'org ont reçu leur propre courriel."},
           "mois": ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
                    "sept.", "oct.", "nov.", "déc.")},
    "en": {"un": "{agent} stopped", "n": "{n} agents stopped",
           "faire": "Do it now", "agent": "Agent", "agents": "Agents",
           "connecteur": "Connector", "connecteurs": "Connectors",
           "depuis": {"echec": "Failing since", "forfait": "Not running since"},
           "passages": {"echec": "{n} failed runs", "forfait": "{n} run(s) missed"},
           "erreur": "Error",
           "fin": {"admin": "You are receiving this email as an administrator of this "
                            "workspace. It is the only one about this. Turn an agent off "
                            "in Agents if you no longer need it.",
                   "membre": "You are receiving this email because you have access to "
                             "these agents. It is the only one about this. Turn an agent "
                             "off in Agents if you no longer need it.",
                   "equipe": "Copy for the platform team: the people of the org received "
                             "their own email."},
           "mois": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                    "Nov", "Dec")},
}


def sujet_par_probleme(blocs: list[dict], en: bool) -> str:
    """Le sujet ET le titre : l'agent arrêté quand il est seul, leur nombre sinon."""
    p = _PAR_PROBLEME["en" if en else "fr"]
    agents = [a for b in blocs for a in b["agents"]]
    if len(agents) == 1:
        return p["un"].format(agent=agents[0]["label"])
    return p["n"].format(n=len(agents))


def _quand(depuis: str, p: dict, en: bool) -> str:
    """« Nov 15, 02:00 UTC » / « 15 nov., 02:00 UTC » ; tel quel s'il ne se lit pas."""
    try:
        an, mois, jour = depuis[:10].split("-")
        mot = p["mois"][int(mois) - 1]
    except (ValueError, IndexError):
        return depuis
    date = f"{mot} {int(jour)}" if en else f"{int(jour)} {mot}"
    return f"{date}, {depuis[11:16]} UTC" if len(depuis) >= 16 else date


def _courriel_par_probleme(m, to: str, blocs: list[dict], *, locale: Optional[str],
                           en: bool, qualite: str, entete: Optional[str] = None) -> bool:
    """Le courriel sous le réglage des alertes : un titre, puis un bloc par problème — le
    texte, le geste, la table des détails, le bouton. Tout texte est échappé : il vient
    du médecin, d'un opérateur, ou d'un fournisseur."""
    from . import email as _email
    from . import email_brand as _charte
    from .email_templates import _envoyer

    esc, attr = _email._esc, _email._esc_attr
    p = _PAR_PROBLEME["en" if en else "fr"]
    discret = _charte.discret(m)
    mono = ("font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;"
            f"color:{m.discret}")

    def _nom(a: dict) -> str:
        nom = esc(a["label"])
        return (f'<a href="{attr(a["url"])}" style="color:{m.encre};font-weight:600">'
                f'{nom}</a>') if a.get("url") else f"<strong>{nom}</strong>"

    def _table(lignes: list[tuple[str, Optional[str]]]) -> str:
        tr = "".join(
            f'<tr><td style="padding:3px 16px 3px 0;vertical-align:top;white-space:nowrap;'
            f'{discret}">{esc(k)}</td><td style="padding:3px 0;vertical-align:top;'
            f'font-size:14px;line-height:1.45">{v}</td></tr>' for k, v in lignes if v)
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                f'style="margin:12px 0 0">{tr}</table>')

    sujet = sujet_par_probleme(blocs, en)
    corps = [f'<p style="margin:0 0 8px;font-size:20px;line-height:1.3;font-weight:600">'
             f'{esc(sujet)}</p>']
    if entete:
        corps.insert(0, f'<p style="margin:0 0 12px"><strong>{esc(entete)}</strong></p>')
        sujet = f"[équipe] {sujet}"
    for i, b in enumerate(blocs):
        filet = "" if i == 0 else f"border-top:1px solid {m.filet};"
        depuis = None
        if b.get("since"):
            depuis = esc(_quand(b["since"], p, en))
            if b.get("runs"):
                depuis += " · " + esc(p["passages"][b["type"]].format(n=b["runs"]))
        connecteurs = b.get("connectors") or []
        corps.append(
            f'<div style="{filet}padding:20px 0 0;margin-top:{0 if i == 0 else 20}px">'
            f'<p style="margin:0 0 4px;font-weight:600">{esc(b["headline"])}</p>'
            + (f'<p style="margin:0">{esc(b["todo"])}</p>' if b.get("todo") else "")
            + _table([
                (p["agents"] if len(b["agents"]) > 1 else p["agent"],
                 "<br>".join(_nom(a) for a in b["agents"])),
                (p["connecteurs"] if len(connecteurs) > 1 else p["connecteur"],
                 esc(", ".join(connecteurs)) if connecteurs else None),
                (p["depuis"][b["type"]], depuis),
                (p["erreur"],
                 f'<span style="{mono}">{esc(b["error"])}</span>' if b.get("error") else None)])
            + (_charte.bouton(m, b["link"], p["faire"]) if b.get("link") else "")
            + "</div>")
    corps.append(f'<p style="margin:24px 0 0;{discret}">{esc(p["fin"][qualite])}</p>')
    return _envoyer(m, to, sujet, _charte.page(
        m, "".join(corps), preheader=blocs[0]["headline"] if blocs else sujet,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def _courriel(to: str, agents: list[dict], *, brand: Optional[str],
              locale: Optional[str], entete: Optional[str] = None,
              org_id: Optional[int] = None, sub: Optional[str] = None,
              par_probleme: bool = False, qualite: str = "admin") -> bool:
    """UN message à UN destinataire, pour les agents bloqués d'une org qu'il voit.
    `entete` : la ligne du courriel d'ÉQUIPE (`alertes_equipe.entete`), en tête du
    corps et de l'objet ; absente du courriel d'un client.

    `par_probleme` (réglage des alertes allumé ; `org_id`, `sub` et `qualite` du
    destinataire, `destinataires_de_l_alerte`) : il parle comme Slack (`problemes`).
    Sinon, le courriel d'avant : lequel, depuis quand, combien de passages perdus, ce
    que dit le dernier refus — et les gestes possibles.

    Un agent qui ATTEND un abonnement à reconnecter (`forfait`, `agents_sans_forfait`)
    n'échoue pas : il ne part plus. Sa ligne le dit, nomme l'abonnement par le libellé
    de sa famille (`runner_models.libelle_abonnement`), et le geste est nommé —
    reconnecter l'abonnement, ce que seule la personne qui le paie peut faire."""
    from . import email as _email
    from . import email_brand as _charte
    from . import runner_models
    from .email_templates import _envoyer

    m = _charte.marque(brand)
    locale = locale or m.langue or None
    en = locale == "en"
    if par_probleme and org_id is not None:
        blocs = problemes(int(org_id), agents, "en" if en else "fr", marque=m.nom, sub=sub)
        return _courriel_par_probleme(m, to, blocs, locale=locale, en=en, qualite=qualite,
                                      entete=entete)
    esc = _email._esc
    items = []
    abonnements: list[str] = []
    for a in agents:
        depuis = str(a.get("depuis") or "")[:16]
        if a.get("forfait"):
            nom = runner_models.libelle_abonnement(a["famille"], en=en)
            abonnements.append(nom)
            if en:
                items.append(f"<li><strong>{esc(a['libelle'])}</strong>: has not run since "
                             f"{esc(depuis)} UTC ({a['travaux']} run(s) missed) — to "
                             f"reconnect: {esc(nom)}</li>")
            else:
                items.append(f"<li><strong>{esc(a['libelle'])}</strong> : ne tourne plus "
                             f"depuis le {esc(depuis)} UTC ({a['travaux']} passage(s) "
                             f"manqué(s)) — à reconnecter : {esc(nom)}</li>")
            continue
        motif = f" — « {esc(a['motif'])} »" if a.get("motif") else ""
        if en:
            items.append(f"<li><strong>{esc(a['libelle'])}</strong>: {a['travaux']} runs "
                         f"in a row have failed since {esc(depuis)} UTC{motif}</li>")
        else:
            items.append(f"<li><strong>{esc(a['libelle'])}</strong> : {a['travaux']} "
                         f"passages de suite en échec depuis le {esc(depuis)} UTC{motif}</li>")
    liste = f'<ul style="{_charte.PARA}">{"".join(items)}</ul>'
    en_echec = any(not a.get("forfait") for a in agents)
    noms = esc(", ".join(dict.fromkeys(abonnements)))
    p = f'<p style="{_charte.PARA}">'
    if en:
        subject = "A scheduled agent is blocked"
        tete = "are not running:" if abonnements else "keep failing, run after run:"
        corps = f"{p}These scheduled agents of your organization {tete}</p>" + liste
        if en_echec:
            corps += (f"{p}They will keep starting on schedule and fail the same way until "
                      "the cause is fixed (often a key refused by its provider): fix it, "
                      "or turn the agent off.</p>")
        if abonnements:
            corps += (f"{p}An agent that runs on a personal plan ({noms}) waits until that "
                      "plan is reconnected: its owner reconnects it in Connectors, or "
                      "switches the agent to another model.</p>")
        corps += f"{p}This is the only message for this outage.</p>"
    else:
        subject = "Un agent programmé est bloqué"
        tete = "ne fonctionnent plus :" if abonnements else "échouent passage après passage :"
        corps = f"{p}Ces agents programmés de votre organisation {tete}</p>" + liste
        if en_echec:
            corps += (f"{p}Ils continueront de partir à l'heure et d'échouer de la même "
                      "façon tant que la cause n'est pas réparée (souvent une clé refusée "
                      "par son fournisseur) : réparez-la, ou coupez l'agent.</p>")
        if abonnements:
            corps += (f"{p}Un agent posé sur un abonnement personnel ({noms}) attend que cet "
                      "abonnement soit reconnecté : la personne à qui il appartient le "
                      "reconnecte dans Connecteurs, ou passe l'agent sur un autre "
                      "modèle.</p>")
        corps += f"{p}C'est le seul message pour cette panne.</p>"
    if entete:
        corps = f'<p style="{_charte.PARA}"><strong>{esc(entete)}</strong></p>' + corps
        subject = f"[équipe] {subject}"
    return _envoyer(m, to, subject, _charte.page(
        m, corps, preheader=subject,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def _soignes_par_nous(trigger_ids: list) -> set[int]:
    """Les agents dont la panne est du côté PLATEFORME (`medecin_agents.py`) ET que la
    boucle tient encore (épisode vu depuis moins de `medecin_agents.FRAICHEUR`) : leur
    titulaire n'y peut rien, on ne lui écrit pas. Une boucle éteinte ou arrêtée cesse de
    voir ses épisodes, et l'alerte reprend d'elle-même. Ouvert sur erreur — une lecture
    ratée laisse partir le courriel : prévenir pour rien vaut mieux que se taire à tort."""
    from . import medecin_agents
    from .db import episodes_agents
    try:
        return episodes_agents.agents_soignes_par_nous(
            trigger_ids, vus_depuis=medecin_agents.FRAICHEUR)
    except Exception:  # noqa: BLE001
        log.warning("alerte agents bloqués : épisodes illisibles — aucun agent écarté",
                    exc_info=True)
        return set()


def _annonces_sur_slack(trigger_ids: list) -> set[int]:
    """Les agents dont l'épisode vivant a déjà été annoncé sur le Slack de l'org
    (`alertes_slack_agents`) : le courriel serait le même message, en moins utile. Il
    reste le filet quand Slack n'est pas réglé ou a refusé (rien n'est alors marqué).
    L'appelant ne le demande que si l'instance a allumé ces alertes."""
    from .db._conn import _connect
    if not trigger_ids:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT trigger_id FROM agent_episodes WHERE statut <> 'clos' "
            "AND slack_alerte_at IS NOT NULL AND trigger_id = ANY(%s)",
            ([int(t) for t in trigger_ids],)).fetchall()
    return {int(r["trigger_id"]) for r in rows}


def _sans_forfait() -> list[dict]:
    """`alertes_agents.agents_sans_forfait`, sans faire tomber le passage : une lecture
    ratée laisse partir l'alerte des agents en échec, et le dit."""
    from .db import alertes_agents
    try:
        return alertes_agents.agents_sans_forfait()
    except Exception:  # noqa: BLE001
        log.warning("alerte agents bloqués : agents en attente d'abonnement illisibles",
                    exc_info=True)
        return []


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : relève les épisodes à annoncer, écrit à chaque destinataire de l'org
    le courriel de CE qu'il a le droit de voir, puis à l'équipe un courriel séparé, et
    marque ce qui est parti. Synchrone (la boucle le pousse hors de l'event loop).

    L'équipe = les admins de plateforme de l'instance (`alertes_equipe.destinataires`) ;
    sans aucun, une ligne ERROR par org et aucun courriel d'équipe — les clients
    reçoivent le leur."""
    from . import alertes_equipe, org_store
    from .db import alertes_agents
    from .db import users as db_users

    maintenant = maintenant or datetime.now(timezone.utc)
    if _heures_calmes(maintenant):
        return {"heures_calmes": True}
    equipe: Optional[list] = None   # lue à la première org à écrire
    bloques = alertes_agents.agents_bloques(SEUIL)
    soignes = _soignes_par_nous([a["trigger_id"] for a in bloques])
    from . import alertes_slack_agents
    par_probleme = alertes_slack_agents.actives()
    if par_probleme:
        soignes |= _annonces_sur_slack([a["trigger_id"] for a in bloques])
    par_org: dict[int, list[dict]] = {}
    vus: set[int] = set()
    for a in bloques:
        if int(a["trigger_id"]) in soignes:
            continue
        vus.add(int(a["trigger_id"]))
        par_org.setdefault(int(a["org_id"]), []).append(a)
    # Les agents qui ATTENDENT un abonnement à reconnecter : leurs travaux ne meurent
    # pas, l'épisode ci-dessus ne les voit jamais. Un agent déjà nommé l'est une fois —
    # et ses DEUX épisodes sont marqués, puisque le message l'a nommé.
    en_attente: set[int] = set()
    for a in _sans_forfait():
        tid = int(a["trigger_id"])
        en_attente.add(tid)
        if tid in vus:
            continue
        vus.add(tid)
        par_org.setdefault(int(a["org_id"]), []).append(a)
    envoyes, lettres, marques, ecartees, sans_destinataire = 0, 0, 0, 0, 0
    equipe_partis, equipe_rates = 0, 0
    for org_id, agents in sorted(par_org.items()):
        if not org_a_nous(org_id, qui="alerte agents bloqués"):
            ecartees += 1
            if org_id not in _ECARTEES_DITES:
                _ECARTEES_DITES.add(org_id)
                log.info("alerte agents bloqués : org %s d'un tenant tiers écartée "
                         "(%d agent(s) bloqué(s), aucun courriel)", org_id, len(agents))
            continue
        if equipe is None:
            equipe = alertes_equipe.destinataires()
        dest = destinataires_de_l_alerte(org_id, agents, equipe=equipe)
        clients = [d for d in dest if d["qualite"] != "equipe"]
        if not clients:
            # On ne marque PAS : le jour où l'org gagne un admin joignable, l'épisode part.
            sans_destinataire += 1
            continue
        _base, marque = org_store.org_front(org_id)
        partis = 0
        for d in clients:
            if not d["agents"]:
                continue
            fiche = db_users.get_user(d["sub"]) or {}
            if _courriel(d["adresse"], d["agents"], brand=marque,
                         locale=fiche.get("locale"), org_id=org_id, sub=d["sub"],
                         par_probleme=par_probleme, qualite=d["qualite"]):
                partis += 1
        if not partis:
            log.warning("alerte agents bloqués : aucun courriel parti pour l'org %s — "
                        "rejoué au passage suivant", org_id)
            continue
        # Marqué dès qu'UN destinataire l'a reçu : rejouer pour celui qu'un envoi a
        # raté renverrait le même message à tous les autres. Un agent qu'aucun client
        # ne voit est marqué aussi : l'équipe, elle, le reçoit.
        envoyes += 1
        lettres += partis
        # Deux marques, une par épisode : un agent annoncé « à reconnecter » reste
        # annonçable s'il se met ensuite à échouer, et inversement.
        echecs = [a["trigger_id"] for a in agents if not a.get("forfait")]
        attentes = [a["trigger_id"] for a in agents
                    if a.get("forfait") or int(a["trigger_id"]) in en_attente]
        marques += (alertes_agents.marquer_annonces(echecs)
                    + alertes_agents.marquer_forfait_annonces(attentes))
        # L'équipe : un courriel SÉPARÉ, une fois par épisode annoncé. Un envoi raté
        # est dit et compté, jamais rejoué (l'épisode est marqué pour le client).
        if not equipe:
            alertes_equipe.signaler_sans_equipe("alerte agents bloqués", org_id)
            continue
        entete = alertes_equipe.entete(org_id, partis)
        for d in dest:
            if d["qualite"] != "equipe":
                continue
            if _courriel(d["adresse"], d["agents"], brand=None, locale="fr", entete=entete,
                         org_id=org_id, sub=None, par_probleme=par_probleme,
                         qualite="equipe"):
                equipe_partis += 1
            else:
                equipe_rates += 1
                log.warning("alerte agents bloqués : courriel d'équipe refusé pour "
                            "l'org %s (non rejoué)", org_id)
    return {"orgs": len(par_org), "envoyes": envoyes, "lettres": lettres,
            "marques": marques, "equipe": equipe_partis, "equipe_rates": equipe_rates,
            "tenant_tiers_ecartes": ecartees, "sans_destinataire": sans_destinataire}


async def run_alerte_agents_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True`) : un passage par
    `interval`, hors de l'event loop. Un passage raté ne tue pas la boucle — il le dit."""
    log.info("alerte agents bloqués : démarrée (seuil %s, passage toutes les %ss).",
             SEUIL, interval)
    from .db import verrou_de_boucle
    while True:
        try:
            # La marque vient APRÈS l'envoi : deux processus sur la même base (bleu/vert)
            # liraient les mêmes épisodes non marqués et écriraient deux fois. Un seul
            # joue le passage (`db/verrou_de_boucle.py`).
            bilan = await asyncio.to_thread(verrou_de_boucle.sous_verrou,
                                            "alerte_agents_bloques", balayer, si_saute={})
            if bilan.get("envoyes"):
                log.info("alerte agents bloqués : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("alerte agents bloqués : passage en échec : %s", e)
        await asyncio.sleep(interval)

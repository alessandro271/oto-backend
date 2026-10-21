"""Outils MCP RETIRÉS — le nom ne répond plus : il REFUSE en nommant le geste qui aboutit.

Un retrait n'est pas un renommage (`deprecations.TOOLS`) : il n'y a pas de nom
d'aujourd'hui vers lequel traduire l'appel, donc rien à servir « à côté ». Ce n'est pas
non plus un nom simplement inconnu : `error_taxonomy._surviving_siblings` DÉRIVE la
fratrie d'un nom disparu, ce qui suffit quand ses verbes ont été rangés sous un voisin
(`gmail_search` → `gmail_message`, aujourd'hui `gmail`). Pour un retrait, la fratrie serait les ~60 outils
`oto_*` — une liste qui n'apprend rien — alors que le geste qui aboutit est connu et
précis. Il s'écrit donc, une fois, ICI, et trois surfaces le lisent :

- `error_taxonomy.classify` — un `tools/call` direct sur le nom (fastmcp : « Unknown
  tool ») ;
- `tools/meta.oto_call` — AVANT le refus méta/spine : un nom `oto_*` y recevait « appelle-le
  directement », qui renvoie au chemin précédent. Une boucle ;
- `tools/meta.oto_tool_schema` — ce que l'agent lit avant d'appeler.

⚠️ **Le refus doit être AUTOSUFFISANT.** Il est lu par des agents qu'une procédure ou une
page d'org — contenu client, qu'on ne corrige pas — envoie encore vers ce nom : aucun
renvoi à un guide, aucune hypothèse sur ce que l'agent a lu avant.

Un retrait peut aussi REMPLACER un nom sans le servir à côté (`RENOMMAGES`, depuis le 17/10/2026) :
quand l'ancien nom est lui-même la faille, il n'y a pas d'alias à tenir pendant un préavis —
il refuse tout de suite, en nommant le nouvel appel.

Module PUR (aucun import `oto_mcp`) : la taxonomie d'erreurs doit rester importable seule.
"""
from __future__ import annotations

import datetime
from typing import NamedTuple, Optional


class Retrait(NamedTuple):
    #: Ce que l'agent lit : ce qui a disparu, pourquoi rien n'est perdu, et le geste.
    message: str
    #: Le `hint` de l'enveloppe d'erreur (`data.oto.hint`) : le geste seul.
    hint: str


#: `oto_kb` — retiré le 10/09/2026 (décision d'Alexis) : la « base de connaissance » n'existe
#: plus comme concept, il n'y a que projet, doc et procédure. La DONNÉE ne bouge pas : c'était
#: déjà un projet ordinaire. La route REST `POST /api/me/kb` reste, le tableau de bord la
#: consomme (cf. `capabilities/kb.py`).
KB = Retrait(
    message=(
        "`oto_kb` is retired: there is no org \"knowledge base\" any more — oto has "
        "projects, docs and procedures, nothing else. What was called the knowledge base "
        "is an ordinary PROJECT of the org; nothing was moved or deleted, its pages are all "
        "still there. Reach them like any project's pages: find the project with "
        "`oto_project op=list` (seeded as \"Knowledge base\", or \"Base de connaissance\" in "
        "older orgs, and possibly renamed since), then read or write its pages with "
        "`oto_doc` and that `project_id` (`op=search`, `op=get`, `op=create`, "
        "`op=update`). Durable knowledge you capture goes, as a doc, into the project it "
        "belongs to. If a procedure or a page told you to call `oto_kb`, it predates this "
        "retirement: take the path above instead — this name only returns this refusal, "
        "called directly or through `oto_call`."),
    hint=("find the project with oto_project op=list, then read or write its pages with "
          "oto_doc and its project_id"),
)

# --- 17/10/2026 : lire ou simuler (`ns`), agir (`ns_act`) --------------------------------
# Décision d'Alexis, pour la sécurité : un client MCP autorise des OUTILS, pas des
# arguments. `gmail_compose(mode="send")` envoyait sous l'autorisation du brouillon,
# `gmail_message(op="trash")` vidait une boîte sous celle de la lecture. Chaque connecteur
# sert désormais DEUX outils : `<ns>` lit ou simule (un brouillon est une simulation),
# `<ns>_act` engage ou détruit (envoi, corbeille, archivage, déplacement) — c'est lui que
# le client fait confirmer. Coupure IMMÉDIATE, sans transition (exception à l'Art 8.2,
# `docs/alias-deprecies.md`).
#
# 21/10/2026, même décision pour TOUTES les messageries : un agent a envoyé un WhatsApp
# sans validation parce que `whatsapp_chat` (le lecteur, en « toujours autoriser » dans
# un projet) servait encore son ancienne `op="send"`. Les lecteurs de messagerie et leurs
# outils d'envoi deviennent des paires — `whatsapp`/`whatsapp_act`, `telegram`/
# `telegram_act`, `instagram`/`instagram_act`, `linkedin_unipile`/`linkedin_unipile_act`
# (la messagerie LinkedIn), `teams`/`teams_act`, `chat`/`chat_act` (Google Chat) — et
# leurs anciennes ops d'envoi en transition (`deprecations.RETRAIT_OPS_ENVOI`)
# disparaissent avec les anciens noms.
#
# **Une table de correspondance par lot**, et `RENOMMAGES` les réunit : le refus nommé de
# chaque ancien nom et l'annonce servie aux agents (`annonce_act`) dérivent de
# `RENOMMAGES` ; la migration des listes d'outils enregistrées lit la table de SON lot —
# `RENOMMAGES_ACT` pour la révision 0062 (déjà jouée : cette table est FIGÉE),
# `RENOMMAGES_MESSAGERIES` pour `20261021_1219_outils_act_msg`. Une ligne ajoutée est
# annoncée, refusée et migrée ; rien ne se recopie ailleurs.
#
# **Le sens d'une liste réécrite** (décision d'Alexis du 21/10/2026, à partir du lot des
# messageries) : une liste qui AUTORISE ne reçoit, pour un ancien outil MIXTE (qui lisait
# et, en transition, envoyait encore), que le nouveau lecteur — jamais `<ns>_act` ; seul
# un ancien outil d'envoi DÉDIÉ donne `<ns>_act` (`remplacants_autorises`). Une liste qui
# MASQUE masque tout ce que l'ancien outil faisait (`remplacants`) : masquer reste large,
# autoriser reste étroit. Le lot Gmail/Outlook (0062), joué avant cette règle, garde la
# sienne : ses deux sens prenaient tous les remplaçants.


#: Les jours des lots : Gmail et Outlook, puis les messageries.
LOT_MAILS = datetime.date(2026, 10, 17)
LOT_MESSAGERIES = datetime.date(2026, 10, 21)


class Renommage(NamedTuple):
    ancien: str                 # l'outil retiré
    selecteur: Optional[str]    # ce qui choisissait le geste : "op=…", "mode=…", None = tout l'outil
    outil: str                  # le nouvel outil
    op: str                     # son op
    depuis: datetime.date       # le jour du retrait


def _ops(ancien: str, cible: str, ops: tuple, depuis: datetime.date) -> tuple:
    return tuple(Renommage(ancien, f"op={o}", cible, o, depuis) for o in ops)


#: Le lot du 17/10/2026, que la révision 0062 lit. FIGÉ : 0062 est jouée.
RENOMMAGES_ACT: tuple[Renommage, ...] = (
    Renommage("gmail_list_accounts", None, "gmail", "accounts", LOT_MAILS),
    *_ops("gmail_message", "gmail", ("search", "get", "attachment", "drafts"), LOT_MAILS),
    *_ops("gmail_message", "gmail_act", ("archive", "trash"), LOT_MAILS),
    Renommage("gmail_compose", "mode=draft", "gmail", "draft", LOT_MAILS),
    Renommage("gmail_compose", "mode=send", "gmail_act", "send", LOT_MAILS),
    *_ops("outlook_message", "outlook", ("search", "get", "attachment", "drafts", "folders"),
          LOT_MAILS),
    *_ops("outlook_message", "outlook_act", ("archive", "trash", "move"), LOT_MAILS),
    Renommage("outlook_compose", "mode=draft", "outlook", "draft", LOT_MAILS),
    Renommage("outlook_compose", "mode=send", "outlook_act", "send", LOT_MAILS),
)


def _messagerie_unipile(canal: str) -> tuple:
    """Un canal de messagerie Unipile (`register_messaging_tools`) : `<canal>_chat`
    (op=list/read, et l'ancienne op=send) et `<canal>_send` → `<canal>` / `<canal>_act`."""
    return (*_ops(f"{canal}_chat", canal, ("list", "read"), LOT_MESSAGERIES),
            Renommage(f"{canal}_chat", "op=send", f"{canal}_act", "send", LOT_MESSAGERIES),
            Renommage(f"{canal}_send", None, f"{canal}_act", "send", LOT_MESSAGERIES))


#: Le lot des messageries (21/10/2026), que la révision `20261021_1219_outils_act_msg` lit.
RENOMMAGES_MESSAGERIES: tuple[Renommage, ...] = (
    *_messagerie_unipile("whatsapp"),
    *_messagerie_unipile("telegram"),
    *_messagerie_unipile("instagram"),
    *_ops("linkedin_unipile_chat", "linkedin_unipile", ("list", "read", "attendees",
                                                        "contacts"), LOT_MESSAGERIES),
    *_ops("linkedin_unipile_chat", "linkedin_unipile_act", ("send", "update", "react"),
          LOT_MESSAGERIES),
    *_ops("linkedin_unipile_send", "linkedin_unipile_act", ("send", "update", "react"),
          LOT_MESSAGERIES),
    *_ops("teams_spaces", "teams", ("teams", "channels", "chats"), LOT_MESSAGERIES),
    *_ops("teams_message", "teams", ("list", "replies"), LOT_MESSAGERIES),
    *_ops("teams_message", "teams_act", ("post", "reply"), LOT_MESSAGERIES),
    *_ops("teams_send", "teams_act", ("post", "reply"), LOT_MESSAGERIES),
    Renommage("chat_spaces", None, "chat", "spaces", LOT_MESSAGERIES),
    Renommage("chat_message", "op=list", "chat", "list", LOT_MESSAGERIES),
    Renommage("chat_message", "op=send", "chat_act", "send", LOT_MESSAGERIES),
    Renommage("chat_send", None, "chat_act", "send", LOT_MESSAGERIES),
)

#: Tous les lots : ce que le refus et l'annonce lisent.
RENOMMAGES: tuple[Renommage, ...] = RENOMMAGES_ACT + RENOMMAGES_MESSAGERIES

#: L'annonce est servie aux agents (`instructions`, section « Ton contexte oto » : au
#: handshake et dans `oto_context`) jusqu'à ce jour inclus — deux mois après le dernier
#: lot, la durée du préavis qu'elle remplace.
ANNONCE_ACT_JUSQU_AU = datetime.date(2026, 12, 21)


def remplacants(ancien: str) -> tuple[str, ...]:
    """Les nouveaux outils qui reprennent TOUT ce que faisait `ancien` — le sens d'une
    liste qui MASQUE (et des deux sens au lot 0062). `()` si `ancien` n'est pas renommé."""
    return tuple(dict.fromkeys(r.outil for r in RENOMMAGES if r.ancien == ancien))


def remplacants_autorises(ancien: str) -> tuple[str, ...]:
    """Ce qu'une liste qui AUTORISE `ancien` autorise désormais : un ancien outil MIXTE
    (qui avait un lecteur parmi ses remplaçants) ne donne que la lecture, jamais
    `<ns>_act` ; un ancien outil d'envoi dédié donne `<ns>_act`."""
    tous = remplacants(ancien)
    lecteurs = tuple(o for o in tous if not o.endswith("_act"))
    return lecteurs or tous


def _retrait_act(ancien: str) -> Retrait:
    lignes = [r for r in RENOMMAGES if r.ancien == ancien]
    gestes = "; ".join(
        (f"{r.selecteur} → " if r.selecteur else "")
        + f'`{r.outil}(op="{r.op}", …)`' for r in lignes)
    lecteur = lignes[0].outil.removesuffix("_act")
    acteur = f"{lecteur}_act"
    return Retrait(
        message=(
            f"`{ancien}` is retired ({lignes[0].depuis:%d/%m/%Y}, security): an MCP client "
            "authorizes "
            f"TOOLS, not arguments, so a tool that reads must not also send or delete. "
            f"`{lecteur}` reads and simulates (a draft is a simulation); `{acteur}` acts — "
            f"it sends, deletes or changes, and your client asks you to confirm it. Call "
            f"instead, with the same "
            f"other arguments: {gestes}. If a procedure or a page told you to call "
            f"`{ancien}`, it predates this change: take the path above — this name only "
            f"returns this refusal, called directly or through `oto_call`."),
        hint=gestes,
    )


def annonce_act_du_jour(aujourd_hui: Optional[datetime.date] = None) -> Optional[str]:
    """`annonce_act()` tant qu'elle est servie, None ensuite."""
    jour = aujourd_hui or datetime.date.today()
    return annonce_act() if jour <= ANNONCE_ACT_JUSQU_AU else None


def annonce_act() -> str:
    """La ligne servie aux agents : « ces outils ont changé », ancien → nouveau, lot par
    lot."""
    lots: dict[datetime.date, dict[str, None]] = {}
    for r in RENOMMAGES:
        lots.setdefault(r.depuis, {})[r.ancien] = None
    return ("Tools changed (security: reading and acting are two tools) — "
            + " ; ".join(
                f"on {jour.isoformat()}: "
                + " · ".join(f"`{a}` → {' + '.join(f'`{o}`' for o in remplacants(a))}"
                             for a in anciens)
                for jour, anciens in lots.items())
            + ". The old names refuse, naming the call to make instead.")


RETIRES: dict[str, Retrait] = {
    "oto_kb": KB,
    **{a: _retrait_act(a) for a in dict.fromkeys(r.ancien for r in RENOMMAGES)},
}


def retrait(nom: str) -> Optional[Retrait]:
    """Le refus servi pour un nom CANONIQUE retiré, ou None. L'appelant canonicalise
    d'abord (préfixe de tenant) : `acme_kb` d'un tenant arrive ici en `oto_kb`."""
    return RETIRES.get(nom)

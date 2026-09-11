"""Compteur des refus du TRANSPORT MCP — les requêtes qu'on refuse sans jamais le savoir.

**Le trou mesuré** (11/09/2026). Une requête refusée par le transport du SDK `mcp`
l'est **avant tout dispatch de session** : elle ne traverse aucun middleware FastMCP,
donc elle n'est ni dans `tool_calls`, ni dans Sentry, ni dans aucune télémétrie bâtie
sur les hooks. La seule trace est la ligne de statut du journal d'accès uvicorn — qui
ne porte QUE le code HTTP, dont la rétention utile est de deux jours, et qu'il faut
aller chercher sous les unités COLORÉES (`oto-mcp@blue`, `oto-mcp-canari@green`) et
non sous `oto-mcp`, sous peine de conclure « aucun journal », ce qui est un zéro
crédible et faux.

**Ce que ça pèse** : ~**2,2 %** des `POST /mcp` de production sont refusés en 400
(69 sur 3 117 sur les 8 h suivant un démarrage), en régime **permanent** — ce n'est
pas un incident. ⚠️ **Toutes les adresses sources mesurées sont dans
`160.79.106.0/24`, la plage de sortie de claude.ai. Aucune ne vient de nos propres
machines.** À écrire ici parce que la première lecture de ce compteur, dans un mois,
ressemblera sinon à une panne interne : ce sont des clients tiers qui parlent mal, pas
nous. Et chaque 400 laissait derrière lui une session orpheline (le SDK ne la jetait
pas ; `mcp` 1.30.0 le corrige).

**Pourquoi la cause ne peut se lire QUE dans le corps de la réponse.** Il existe une
vingtaine de points de refus dans le transport, dont **cinq** rendent 400 : erreur
d'analyse, erreur de validation, `Mcp-Session-Id` manquant, version de protocole non
supportée, et `Content-Type` refusé. Le code HTTP ne les sépare donc pas — et le code
JSON-RPC non plus (`-32600` couvre « session absente » ET « version refusée »). Seule
la chaîne `error.message` tranche. Un compteur global dirait exactement ce que le
journal d'accès dit déjà : rien.

**Pourquoi ICI, et pas ailleurs.** C'est un middleware ASGI EXTERNE, jumeau de
`client_disconnect_guard`, posé dans `build_root_app` :

- une sous-classe du transport ne verrait ni les refus du *gestionnaire* de sessions
  (404 de session inconnue, 503 de plafond) ni ceux de la couche de sécurité
  (400 `Content-Type`, 413 de corps trop gros) — ils naissent au-dessus d'elle ;
- un middleware FastMCP est précisément la couche que ces refus court-circuitent ;
- la lib `mcp` n'est pas touchée, et ce fichier se retire d'une ligne.

**Trois garanties, qui sont la condition pour qu'un instrument vive sur le chemin du
transport — il voit TOUT le trafic :**

1. **rien n'est bufferisé sur le chemin nominal.** Tant que le statut est < 400, le
   corps n'est jamais accumulé : deux lectures de dict par message ASGI, et c'est tout.
   Le filtre de chemin écarte d'emblée tout ce qui n'est pas le endpoint MCP ;
2. **vocabulaire d'étiquettes FERMÉ.** Une cause inconnue devient `autre` ; on
   n'invente jamais une étiquette à partir du texte du SDK, sinon le cardinal du
   compteur suivrait les formulations de l'amont ;
3. **on n'enregistre jamais le corps.** Celui d'une erreur de validation porte le
   détail pydantic, donc des DONNÉES DU CLIENT. On en extrait une étiquette et, pour
   le seul cas « version refusée », la version demandée — et seulement si elle a la
   forme stricte d'une date. Aucun en-tête, aucun jeton, aucun identifiant de session
   n'est lu ni écrit.

**Où se lit le résultat** : `oto_admin_monitoring op=transport` (et sa face REST
`GET /api/admin/monitoring/transport`), qui rend le décompte par cause, par code HTTP
et **par environnement** — préproduction et production écrivent dans la MÊME base et
`tool_calls.server` est un littéral constant, donc sans l'environnement dans la ligne
les deux trafics seraient additionnés en silence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

#: Le endpoint MCP, servi tel quel sur le host canonique comme sur les sous-domaines
#: de projet (ADR 0032). Égalité EXACTE, pas un `endswith` : le PRM est servi sur
#: `/.well-known/oauth-protected-resource/mcp`, qui se termine aussi par `/mcp`.
_MCP_PATH = "/mcp"

#: Au-delà, on ne lit plus : les corps de refus du SDK tiennent tous très en dessous,
#: et un plafond borne ce qu'un tiers peut nous faire accumuler par requête.
_FENETRE_CORPS = 512

#: Vocabulaire FERMÉ des causes, dans l'ordre où on les cherche. La clé est le
#: marqueur littéral émis par `mcp` (vérifié sur 1.27.2 ET 1.30.0), la valeur est
#: l'étiquette qu'on écrit. Une formulation d'amont qui change fait tomber son cas
#: dans `autre` — ce qui se voit — au lieu de créer une étiquette neuve en silence.
_MARQUEURS: tuple[tuple[str, str], ...] = (
    ("Parse error", "json_illisible"),
    ("Validation error", "message_non_conforme"),
    ("Bad Request: Missing session ID", "session_absente"),
    ("Bad Request: Unsupported protocol version", "version_refusee"),
    ("Invalid Content-Type header", "content_type_refuse"),
    ("Not Found: Session has been terminated", "session_terminee"),
    ("Not Found: Invalid or expired session ID", "session_inconnue"),
    ("Session not found", "session_inconnue"),
    ("Not Acceptable", "accept_refuse"),
    ("Unsupported Media Type", "media_type_refuse"),
    ("Method Not Allowed", "methode_refusee"),
    ("Conflict", "flux_sse_en_double"),
    # Les deux suivants n'existent qu'à partir de `mcp` 1.30.0 (non installée sur la
    # couleur servie au 11/09/2026). Les déclarer d'avance ne coûte rien et évite que
    # le jour de la montée, tout tombe dans `autre` sans qu'on sache pourquoi.
    ("Request body too large", "corps_trop_gros"),
    ("Too many open sessions", "trop_de_sessions"),
)

#: La SEULE donnée variable qu'on accepte d'écrire, et seulement pour
#: `version_refusee` : une date au format du protocole. Tout le reste est jeté.
_VERSION = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

_EN_VOL: set = set()


def _environnement() -> str:
    """`production` / `canari` / `inconnu`. Lu à chaque ligne (bon marché) plutôt que
    figé à l'import : les tests posent et retirent la variable."""
    return (os.environ.get("OTO_SENTRY_ENV", "").strip().lower() or "inconnu")


def cause_du_refus(corps: bytes) -> "tuple[str, str | None]":
    """Rend `(cause, détail)` à partir du corps d'une réponse de refus.

    Le corps arrive tronqué à `_FENETRE_CORPS`, donc son JSON est souvent illisible :
    on cherche le marqueur dans le TEXTE BRUT et pas seulement dans le `message`
    extrait. C'est nécessaire, pas cosmétique — le détail pydantic d'une erreur de
    validation pousse le marqueur au-delà de ce que `json.loads` peut rendre, et sans
    ça `message_non_conforme` tombait dans `autre` (constaté en fabriquant le refus).
    """
    texte = corps[:_FENETRE_CORPS].decode("utf-8", "replace")
    message = texte
    try:
        charge = json.loads(texte)
        if isinstance(charge, dict):
            message = (charge.get("error") or {}).get("message") or texte
    except Exception:  # noqa: SILENT — un corps non-JSON (tronqué, ou texte brut comme
        # `Invalid Content-Type header`) est le cas NORMAL ici, pas une anomalie : on
        # retombe sur le texte, qui est précisément ce que la recherche de marqueur veut.
        # Journaliser ferait une ligne par refus, soit ~168 par jour, pour un non-événement.
        pass
    for marqueur, etiquette in _MARQUEURS:
        if message.startswith(marqueur) or marqueur in texte:
            if etiquette == "version_refusee":
                trouve = _VERSION.search(message)
                return etiquette, (trouve.group(1) if trouve else None)
            return etiquette, None
    return "autre", None


def _ligne(statut: int, methode: str, cause: str, detail: "str | None") -> dict:
    from .calllog import truncated_args

    brut = {"statut": statut, "methode": methode, "env": _environnement()}
    if detail:
        brut["version_demandee"] = detail
    return {
        "server": "oto",
        "kind": "transport",
        # La cause EST le nom du geste : un `group by tool` rend la ventilation.
        "tool": f"refus:{cause}",
        # Toute écriture d'arguments passe par la fabrique commune, **appelée ici même**
        # — c'est elle qui porte la promesse de masquage, et la garde du dépôt la
        # cherche dans la valeur associée à `args`, pas dans une variable en amont.
        # Elle ne change rien à ces valeurs (les nôtres, courtes et closes) ; c'est
        # exactement pour ça qu'on ne s'en dispense pas : une exemption « inoffensive »
        # est ce qui rend la promesse invérifiable.
        "args": truncated_args(brut) or {},
        "ok": False,
        # Aucun `sub` : à cette couche il n'y a pas d'identité — la requête est
        # refusée avant toute vérification de jeton. Ne pas en deviner une.
    }


def _ecrire(ligne: dict) -> None:
    from . import db
    db.insert_tool_call(ligne)


async def _emettre(ligne: dict) -> None:
    try:
        await asyncio.to_thread(_ecrire, ligne)
    except Exception:  # noqa: BLE001 — le compteur ne casse jamais le service
        logger.warning("comptage d'un refus de transport en échec (%s)",
                       ligne.get("tool"), exc_info=True)


def enregistrer(statut: int, methode: str, corps: bytes) -> None:
    """Compte un refus, **best-effort** et hors du chemin de réponse.

    L'écriture part en tâche de fond : la réponse est déjà partie au client quand
    elle s'exécute, et un échec de journalisation ne peut ni ralentir ni casser le
    transport. Hors boucle d'événements (tests), on écrit en direct — même chemin.
    """
    cause, detail = cause_du_refus(corps)
    ligne = _ligne(statut, methode, cause, detail)
    try:
        boucle = asyncio.get_running_loop()
    except RuntimeError:
        try:
            _ecrire(ligne)
        except Exception:  # noqa: BLE001
            logger.warning("comptage d'un refus de transport en échec (%s)",
                           ligne.get("tool"), exc_info=True)
        return
    tache = boucle.create_task(_emettre(ligne))
    _EN_VOL.add(tache)
    tache.add_done_callback(_EN_VOL.discard)


class TransportRefusalCounter:
    """Enveloppe l'app ASGI et compte, avec sa cause, chaque refus du endpoint MCP.

    Placé SOUS `ClientDisconnectGuard` (donc il ne voit pas la réponse que celle-ci
    synthétise quand un client est parti — ce n'est pas un refus) et AU-DESSUS du
    dispatch par Host, donc il couvre les deux instances FastMCP d'un seul geste.

    Le comptage se fait dans l'enveloppe de `send`, au moment du dernier morceau de
    corps — et non après le retour de l'app. C'est délibéré : une exception qui
    remonte vers la garde (un client parti en cours de route) ne doit ni empêcher le
    comptage de ce qui a déjà été envoyé, ni être avalée ici. Elle traverse, intacte.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or (scope.get("path") or "").rstrip("/") != _MCP_PATH:
            await self.app(scope, receive, send)
            return

        etat = {"statut": 0, "corps": b"", "compte": False}
        methode = scope.get("method") or "?"

        async def _send(message):
            genre = message.get("type")
            if genre == "http.response.start":
                etat["statut"] = int(message.get("status") or 0)
            elif genre == "http.response.body" and etat["statut"] >= 400:
                # Accumulation BORNÉE, et seulement sur un refus : le chemin nominal
                # ne passe jamais ici.
                if len(etat["corps"]) < _FENETRE_CORPS:
                    etat["corps"] += message.get("body") or b""
                if not message.get("more_body", False) and not etat["compte"]:
                    etat["compte"] = True
                    try:
                        enregistrer(etat["statut"], methode, etat["corps"])
                    except Exception:  # noqa: BLE001 — jamais au détriment de la réponse
                        logger.warning("comptage d'un refus de transport impossible",
                                       exc_info=True)
            await send(message)

        await self.app(scope, receive, _send)

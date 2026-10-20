"""Le schéma SLACK d'un webhook : la signature `v0`, la poignée de main, le bruit.

Une app Slack abonnée à l'Events API POSTe chaque événement à UNE adresse (la
« Request URL » de son écran *Event Subscriptions*). Elle ne sait pas poser
`Authorization: Bearer`, et elle ne suit pas Standard Webhooks : elle signe avec
le *Signing Secret* de l'app, à sa façon. Ce module tient tout ce qui est propre
à cette façon — `runner_hook` n'en garde que l'aiguillage, comme pour les autres
schémas (`runner_hook.PREREGLAGES`).

## Les trois choses que Slack exige, et ce qu'on en fait

1. **La signature** — `X-Slack-Signature: v0=<hex>` et
   `X-Slack-Request-Timestamp`, HMAC-SHA256 de `v0:{ts}:{corps brut}` avec le
   *Signing Secret* comme clé, tel quel (pas de base64, pas de préfixe).
   https://api.slack.com/authentication/verifying-requests-from-slack
   Jugée sur les OCTETS reçus, comparée à temps constant, ±5 min — les mêmes
   règles que `runner_hook.verifier_signature`, pour les mêmes raisons.

2. **La poignée de main** (`type: url_verification`) — Slack refuse une Request
   URL qui ne lui renvoie pas son `challenge`. On ne la renvoie qu'APRÈS une
   signature valide : sinon la route servirait d'écho à n'importe qui, et
   confirmerait qu'une adresse existe.

3. **Les retentatives** — Slack attend un 2xx sous 3 s, puis retente
   l'événement trois fois (presque aussitôt, ~1 min, ~5 min ; `X-Slack-Retry-Num`). Chaque événement porte un
   `event_id` dans son corps SIGNÉ : c'est l'identifiant de livraison, donc la
   déduplication de `runner_hook.declencher` s'applique sans rien ajouter.

## Le bruit — ce qui n'est jamais un travail

Une app reçoit TOUS les messages des canaux où elle est, y compris les siens.
Un agent qui poste dans un canal où son app écoute se redéclencherait lui-même,
en boucle, à chaque message qu'il écrit. `classer` écarte donc, SANS enfiler :
les enveloppes qui ne sont pas des événements, les messages de bots, ce que
NOTRE app a produit — même posté avec un jeton utilisateur (`app_id` égal à
l'`api_app_id` de l'enveloppe), ou par son utilisateur-bot (une réaction posée
par l'agent) —, et les sous-types qui ne sont pas un humain qui écrit (édition,
suppression, arrivée dans un canal…). Ce qui est écarté rend 200 — un refus
ferait retenter Slack, puis désactiver l'abonnement.

## Ce que l'agent ne reçoit pas

L'enveloppe porte le *Verification Token* historique de l'app (`token`) et la
liste de ses installations (`authorizations`) : `corps_pour_l_agent` les retire
avant l'enfilage, comme `runner_hook.corps_sans_secret` retire le secret de
Lemlist — en `inline` le corps devient l'entrée de l'agent, donc son transcript.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

#: Le NOM du schéma, tel que `runner_hook.Prereglage.schema` le porte.
SCHEMA = "slack_signature"

#: L'écart toléré entre l'horodatage signé et notre horloge, dans les deux sens.
#: Cinq minutes : la valeur que Slack recommande, et celle de Standard Webhooks.
TOLERANCE_HORODATAGE_S = 300

#: Un *Signing Secret* Slack : de l'hexadécimal, 32 caractères aujourd'hui. On
#: accepte plus long (une rotation de format ne doit pas fermer la porte), jamais
#: autre chose : un jeton `xoxb-`, un `whsec_` ou notre `otoh_` collés ici sont
#: une erreur de champ, et l'accepter ferait un agent branché qui refuse tout.
_SECRET = re.compile(r"[0-9a-f]{32,128}")

#: Les sous-types de `message` qui sont un HUMAIN qui écrit. Tout autre sous-type
#: (`message_changed`, `message_deleted`, `channel_join`, `bot_message`…) est un
#: événement sur un message, pas un message — et il n'appelle pas de travail.
#: Liste FERMÉE à dessein : un sous-type nouveau chez Slack est écarté tant que
#: personne n'a décidé qu'il méritait un agent, plutôt que de lancer des runs.
SOUS_TYPES_HUMAINS = frozenset({None, "file_share", "thread_broadcast", "me_message"})


def secret_valide(secret: Optional[str]) -> bool:
    """Le *Signing Secret* a-t-il la forme de celui que Slack affiche ?"""
    return bool(secret) and bool(_SECRET.fullmatch(secret.strip()))


@dataclass(frozen=True)
class SignatureSlack:
    """Ce que Slack envoie pour prouver qui il est. `brut` = le corps tel que reçu."""
    horodatage: str
    signature: str
    brut: bytes


def signature_des_entetes(entetes, brut: bytes) -> Optional[SignatureSlack]:
    """La signature Slack d'une requête, ou None — les deux en-têtes, ou rien."""
    horodatage = entetes.get("x-slack-request-timestamp")
    signature = entetes.get("x-slack-signature")
    if not (horodatage and signature):
        return None
    return SignatureSlack(horodatage.strip(), signature.strip(), brut)


def verifier(secret: str, recue: SignatureSlack,
             maintenant: Optional[float] = None,
             tolerance_s: int = TOLERANCE_HORODATAGE_S
             ) -> Literal["ok", "invalid", "stale"]:
    """`ok`, `invalid` ou `stale`, avec la même sémantique que
    `runner_hook.verifier_signature` : `stale` n'est rendu que pour une signature
    VALIDE — le seul cas où dire pourquoi on refuse n'apprend rien à un inconnu."""
    if not secret:
        return "invalid"
    try:
        horodatage = int(recue.horodatage)
    except ValueError:
        return "invalid"
    base = f"v0:{recue.horodatage}:".encode() + recue.brut
    # `hmac.digest` (une passe) : la vérification d'un webhook, pas une signature de
    # state OAuth — celle-là passe par `oauth_flow` (cliquet de test_oauth_flow).
    attendue = "v0=" + hmac.digest(secret.strip().encode(), base,
                                   hashlib.sha256).hex()
    if not hmac.compare_digest(recue.signature.encode(), attendue.encode()):
        return "invalid"
    maintenant = time.time() if maintenant is None else maintenant
    if abs(maintenant - horodatage) > tolerance_s:
        return "stale"
    return "ok"


@dataclass(frozen=True)
class Classement:
    """Ce qu'une livraison Slack AUTHENTIFIÉE appelle.

    `challenge` posé : la poignée de main, à renvoyer telle quelle. `ignore`
    posé : rien à faire, et pourquoi (rendu à Slack, jamais journalisé — un
    message sur deux d'un canal actif ne remplit pas l'écran du propriétaire).
    Ni l'un ni l'autre : un événement qui part vers l'agent."""
    challenge: Optional[str] = None
    ignore: Optional[str] = None


def classer(corps: Any) -> Classement:
    """Poignée de main, bruit, ou événement — sur un corps DÉJÀ authentifié."""
    if not isinstance(corps, dict):
        return Classement(ignore="not_an_event")
    genre = corps.get("type")
    if genre == "url_verification":
        challenge = corps.get("challenge")
        if isinstance(challenge, str) and challenge:
            return Classement(challenge=challenge[:512])
        return Classement(ignore="empty_challenge")
    if genre != "event_callback":
        # `app_rate_limited` et les enveloppes futures : rien qu'un agent lise.
        return Classement(ignore="not_an_event")
    evt = corps.get("event")
    if not isinstance(evt, dict):
        return Classement(ignore="not_an_event")
    # ⚠️ AVANT tout le reste : le message d'un bot — le nôtre compris — ne lance
    # jamais d'agent. C'est la garde contre la boucle d'un agent qui poste dans
    # un canal où son app écoute.
    if (evt.get("bot_id") or evt.get("bot_profile")
            or evt.get("subtype") == "bot_message"):
        return Classement(ignore="bot_message")
    # Ce que NOTRE app a fait sans passer pour un bot : un message posté avec un
    # jeton UTILISATEUR (il porte l'`app_id` de l'app, pas de `bot_id`), ou un
    # geste de son utilisateur-bot (une réaction posée par l'agent).
    app = corps.get("api_app_id")
    if app and evt.get("app_id") == app:
        return Classement(ignore="own_app")
    if evt.get("user") and evt.get("user") in _utilisateurs_bot(corps):
        return Classement(ignore="own_app")
    if evt.get("hidden"):
        return Classement(ignore="hidden")
    if evt.get("type") == "message" and evt.get("subtype") not in SOUS_TYPES_HUMAINS:
        return Classement(ignore="not_a_new_message")
    return Classement()


def _utilisateurs_bot(corps: dict) -> set:
    """Les utilisateurs-bots de NOTRE app, tels que l'enveloppe les déclare."""
    auths = corps.get("authorizations")
    if not isinstance(auths, list):
        return set()
    return {a.get("user_id") for a in auths
            if isinstance(a, dict) and a.get("is_bot") and a.get("user_id")}


#: Les clés de l'enveloppe que l'agent ne reçoit JAMAIS : le Verification Token
#: historique de l'app et la liste de ses installations.
CLES_RETIREES = ("token", "authorizations")


def corps_pour_l_agent(corps: Any) -> Any:
    """Le corps, AMPUTÉ de ce qui est un credential ou ne regarde pas l'agent.

    ⚠️ Fait APRÈS le classement et le filtre (qui peuvent lire l'enveloppe
    entière) et AVANT l'enfilage : en `inline` le corps devient l'entrée de
    l'agent, donc son transcript et `deliveries with_input`."""
    if not isinstance(corps, dict):
        return corps
    return {k: v for k, v in corps.items() if k not in CLES_RETIREES}


def identifiant(corps: Any) -> Optional[str]:
    """L'`event_id` de l'enveloppe — dans le corps SIGNÉ, donc infalsifiable par
    qui ne détient pas le secret. La clé de déduplication des retentatives."""
    if isinstance(corps, dict) and isinstance(corps.get("event_id"), str):
        return corps["event_id"] or None
    return None

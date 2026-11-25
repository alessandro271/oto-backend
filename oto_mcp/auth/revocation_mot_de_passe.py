"""Un mot de passe changé ferme tout ce qu'il ouvrait : les jetons `oto_` du compte, ses
sessions Logto et leurs autorisations. Un changement de double authentification ferme
la même chose (`revocation_2fa.py`) : ce module reçoit les deux.

## Le trou que ce module ferme

Logto ne révoque rien quand un mot de passe change — ni ses sessions, ni les
autorisations (grants) qui portent les jetons de rafraîchissement — et un jeton d'API
`oto_` ne dépend pas de Logto du tout. Quelqu'un qui a tenu une session une fois
pouvait s'émettre un jeton `oto_` : changer le mot de passe ne le coupait pas.

## Ce qui déclenche, et ce qui ne déclenche pas

Logto POSTe un webhook signé (`api/logto_webhook.py` le reçoit). Deux formes disent un
mot de passe changé, et elles seules :

- `PostResetPassword` — le parcours « mot de passe oublié » a abouti (`userId`) ;
- `User.Data.Updated` émis par la route de management `PATCH /users/:userId/password`
  (un opérateur pose le mot de passe) : la livraison porte `method` et `matchedRoute`.

⚠️ **Le changement par l'Account API (`POST /api/my-account/password`) n'est PAS
reconnaissable** : Logto 1.44 l'annonce par un `User.Data.Updated` qui ne porte que
l'entité utilisateur — sans route, sans méthode, sans champ « mot de passe » —, soit
exactement la livraison d'un changement d'email, de profil ou de double
authentification. Révoquer sur chacune couperait les jetons à chaque retouche de
profil ; on ne le fait pas (`docs/auth-logto.md` §Mot de passe changé). Tout
`User.Data.Updated` qui ne dit pas un mot de passe passe en revanche par le constat de
double authentification (`revocation_2fa.constater`), qui lit l'état des facteurs — et
`PostSignIn` / `PostRegister` aussi : un facteur enrôlé pendant la connexion se voit là.

## La signature

HMAC-SHA256 du CORPS BRUT, clé de signature du webhook (console Logto), digest
hexadécimal dans `logto-signature-sha-256`. Jugée sur les octets reçus, jamais sur un
JSON re-sérialisé. Sans clé déclarée (`OTO_LOGTO_WEBHOOK_SIGNING_KEY`), tout est
refusé : jamais une livraison acceptée sans preuve.

La signature ne porte pas d'horodatage : un corps capturé se rejouerait tel quel. Le
`createdAt` du corps — signé, lui — doit donc tomber dans `FRAICHEUR_S`.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

from .. import db
from ..db import revocations_comptes
from . import annuaire, revocation_2fa

logger = logging.getLogger(__name__)

ENTETE_SIGNATURE = "logto-signature-sha-256"

#: L'âge maximal d'une livraison, jugé sur son `createdAt` signé. Logto retente une
#: livraison échouée dans la minute ; une heure laisse la marge d'une horloge décalée
#: sans rendre un corps capturé rejouable indéfiniment.
FRAICHEUR_S = 3600

#: Qui a coupé, dans la trace d'un jeton révoqué (`user_api_tokens.revoked_by`).
REVOQUE_PAR = "logto-webhook"

#: Les natures de jeton coupées : celles qui portent l'identité du compte. Une clé
#: d'org (`org`) appartient au compte de service de son org, pas à la personne.
NATURES_REVOQUEES = ("user", "delegation")

_ROUTE_MOT_DE_PASSE = "/users/:userId/password"


class Refus(Exception):
    """Une livraison refusée : statut HTTP et code nommé, rendus tels quels."""

    def __init__(self, statut: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.statut, self.code, self.message = statut, code, message or code


def cle_de_signature() -> Optional[str]:
    """La clé de signature du webhook déclarée par l'instance, ou None."""
    return (os.environ.get("OTO_LOGTO_WEBHOOK_SIGNING_KEY") or "").strip() or None


def verifier(brut: bytes, signature: Optional[str], cle: Optional[str]) -> None:
    """Lève `Refus` sauf si `brut` est signé par `cle` (comparaison à temps constant)."""
    if not cle:
        # Notre configuration, pas l'envoyeur : bruyant, et 503 pour que Logto retente
        # une fois la clé posée.
        logger.error("webhook Logto : aucune clé de signature déclarée "
                     "(OTO_LOGTO_WEBHOOK_SIGNING_KEY) — livraison refusée")
        raise Refus(503, "logto_webhook_not_configured",
                    "The instance has no Logto webhook signing key.")
    if not signature:
        raise Refus(401, "invalid_signature", "Missing logto-signature-sha-256.")
    attendue = hmac.new(cle.encode(), brut, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.strip().lower().encode(), attendue.encode()):
        raise Refus(401, "invalid_signature", "Bad logto-signature-sha-256.")


def _frais(evt: dict, maintenant: float) -> float:
    brut = evt.get("createdAt")
    try:
        cree = datetime.fromisoformat(str(brut).replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise Refus(400, "invalid_event", "`createdAt` is missing or not ISO 8601.")
    if abs(maintenant - cree) > FRAICHEUR_S:
        raise Refus(400, "stale_event",
                    f"Delivery created more than {FRAICHEUR_S} s away from now.")
    return cree


def compte_vise(evt: dict) -> Optional[str]:
    """L'identifiant Logto du compte dont le mot de passe vient de changer, ou None si
    la livraison ne dit pas un changement de mot de passe."""
    event = evt.get("event")
    if event == "PostResetPassword":
        return evt.get("userId") or (evt.get("user") or {}).get("id") or None
    if (event == "User.Data.Updated"
            and str(evt.get("method") or "").upper() == "PATCH"
            and str(evt.get("matchedRoute") or "").endswith(_ROUTE_MOT_DE_PASSE)):
        return ((evt.get("params") or {}).get("userId")
                or (evt.get("data") or {}).get("id") or None)
    return None


def revoquer_jetons(sub: str, motif: str, par: str = REVOQUE_PAR) -> int:
    """Révoque tous les jetons `oto_` encore actifs de `sub` (natures
    `NATURES_REVOQUEES`) par le geste unique `db.revoke_api_token` — la trace (qui,
    quand, pourquoi) est celle de toute révocation. Les enfants d'un émetteur passent
    avant lui : sinon sa révocation les emporterait sans qu'ils soient comptés.
    Rend le nombre de jetons coupés."""
    jetons = sorted(db.list_api_tokens(sub, kinds=NATURES_REVOQUEES),
                    key=lambda j: j.get("parent_id") is None)
    return sum(1 for j in jetons
               if db.revoke_api_token(sub, j["id"], revoked_by=par, reason=motif))


def fermer_tout(sub: str, motif: str, par: str = REVOQUE_PAR) -> dict:
    """Pose l'instant « révoqué avant » de `sub`, coupe ses jetons `oto_`, PUIS ferme ses
    sessions et autorisations Logto (si Logto tombe, les jetons sont déjà coupés), puis
    repose l'instant. Rend ce qui a été fermé. Lève `annuaire.FermetureInachevee` — tout
    est idempotent, à refaire.

    L'instant (`db/revocations_comptes.py`, jugé par `session_revoquee.py` à chaque
    requête) est posé DEUX fois :
    - AVANT tout : un JWT d'accès déjà émis — vérifié hors ligne, il survivait jusqu'à
      son terme — et un jeton `oto_` créé entre la liste et la révocation
      (`revoquer_jetons`) sont refusés dès la requête suivante ;
    - APRÈS la fermeture chez Logto : un jeton d'accès rafraîchi pendant la fermeture,
      sur une autorisation pas encore révoquée, est émis après le premier instant ; le
      second le couvre. Ce qui est émis ensuite (la reconnexion) passe."""
    revocations_comptes.poser(sub, motif)
    jetons = revoquer_jetons(sub, motif, par)
    logger.info("%s : %d jeton(s) oto_ révoqué(s) pour %s", motif, jetons, sub)
    logto = annuaire.fermer_sessions(sub)
    logger.info("%s : %d session(s), %d autorisation(s) fermée(s) pour %s",
                motif, logto["sessions"], logto["grants"], sub)
    revocations_comptes.poser(sub, motif)
    return {"revoked_tokens": jetons, "closed_sessions": logto["sessions"],
            "revoked_grants": logto["grants"]}


def traiter(brut: bytes, evt: Any, signature: Optional[str],
            maintenant: Optional[float] = None) -> dict:
    """Une livraison Logto, de la signature à la fermeture. Rend le compte rendu servi
    (200). Lève `Refus` (livraison refusée), `annuaire.LectureInachevee` (facteurs de
    double authentification non lus : rien n'est fermé ni posé, à refaire) ou
    `annuaire.FermetureInachevee` (jetons coupés, Logto pas entièrement fermé : à
    refaire — tout est idempotent)."""
    verifier(brut, signature, cle_de_signature())
    if not isinstance(evt, dict):
        raise Refus(400, "invalid_event", "The body is not a JSON object.")
    _frais(evt, time.time() if maintenant is None else maintenant)
    event = evt.get("event")
    sub = compte_vise(evt)
    if sub is not None:
        return {"ignored": False, "event": event, "reason": "password_changed",
                **fermer_tout(sub, f"mot de passe changé ({event})")}
    sub = revocation_2fa.compte_vise(evt)
    if sub is None:
        return {"ignored": True, "event": event}
    changement, empreinte = revocation_2fa.constater(
        sub, inscription=event == revocation_2fa.INSCRIPTION)
    if not changement:
        revocation_2fa.poser(sub, empreinte)
        return {"ignored": True, "event": event}
    rendu = fermer_tout(sub, f"double authentification changée ({event})")
    # Après la fermeture seulement : en échec, l'écart reste et la relivraison le retrouve.
    revocation_2fa.poser(sub, empreinte)
    return {"ignored": False, "event": event, "reason": "mfa_changed", **rendu}

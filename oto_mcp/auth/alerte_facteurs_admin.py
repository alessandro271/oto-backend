"""L'alerte d'un changement de double authentification sur un compte d'ADMINISTRATION.

Quand les facteurs d'un compte `admin` ou `super_admin` changent (webhook Logto,
`revocation_mot_de_passe.traiter` ; remise à zéro par un opérateur,
`users_admin._reset_mfa`), deux voies, quel que soit `OTO_ADMIN_MFA` :

1. **Un courriel au titulaire du compte** — le seul à savoir si le geste vient de lui —
   et un courriel séparé à chacun des AUTRES admins de la plateforme
   (`alertes_equipe.destinataires`), par la voie des courriels transactionnels de la
   plateforme (`email_templates.send_admin_facteurs_email`, transport `email._send`).
   Quel compte, quel changement, quand, et quoi faire si ce n'est pas lui. Ni facteur,
   ni secret.
2. **Le canal d'exploitation** : une ligne WARNING au journal et un message au suivi
   d'erreurs (Sentry, tag `oto.admin_facteurs`), comme `tenancy._refus` ou
   `medecin_agents._alerte_exploitation`. Une EMPREINTE par événement
   (`["oto.admin_facteurs", sub, quand]`) : chaque changement ouvre son propre ticket et
   notifie, au lieu de s'empiler sous le premier ; une relivraison du même événement
   Logto (même `createdAt`) retombe sur le même.

**Ce qui part chez Sentry, par LISTE BLANCHE.** L'alerte est émise pendant la requête du
webhook : l'intégration Starlette du SDK a lu le corps JSON AVANT le handler et l'attache
(`request.data`) à tout événement capturé pendant cette requête — et ce corps est
l'entité utilisateur de Logto (adresse, téléphone). `send_default_pii=False` ne couvre
pas le corps. Le processeur de l'alerte (`_seulement_l_alerte`, posé sur son propre
scope, donc exécuté APRÈS ceux de la requête) ne garde que les champs de
`_CHAMPS_GARDES` et le seul tag de l'alerte : le message (sub, rôle, événement), le
niveau, l'empreinte, l'horodatage et l'identité du déploiement.

**Un échec d'envoi ne fait pas relivrer.** L'alerte part APRÈS la fermeture des sessions
et la pose de l'empreinte : un 500 ferait relivrer Logto, et chaque relivraison
re-fermerait les sessions du compte sans rien envoyer de plus. Un courriel refusé ou
levé (transport absent compris) se dit donc en ERREUR au journal et au suivi d'erreurs,
et le webhook répond 200. Seule la lecture du RÔLE précède la pose (`role_a_alerter`) :
elle en échec, l'écart reste et la relivraison refait le constat — l'alerte n'est pas
perdue.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

TAG = "oto.admin_facteurs"
TAG_COURRIEL = "oto.admin_facteurs_courriel"

#: Le changement lu par une personne : l'événement Logto, ou le geste d'un opérateur.
REMISE_A_ZERO = "reset_mfa"

#: Les seuls champs d'un événement Sentry de l'alerte qui partent.
_CHAMPS_GARDES = frozenset({
    "event_id", "timestamp", "level", "message", "logentry", "fingerprint",
    "platform", "sdk", "environment", "release", "server_name"})


def _role_plateforme(sub: str) -> str:
    """Le rôle plateforme de `sub` en base, sans la garde de la requête (seam de test)."""
    from .. import access
    return access.role_hors_garde(sub)


def role_a_alerter(sub: str) -> Optional[str]:
    """Le rôle d'administration de `sub` (`admin`, `super_admin`, `OTO_MCP_ADMIN_SUB`
    compris), ou None. Lu AVANT la pose de l'empreinte : une panne lève."""
    from .. import garde_admin
    role = _role_plateforme(sub)
    return role if role in garde_admin.ROLES_BORNES else None


def _seulement_l_alerte(event, hint):
    """Le processeur du scope de l'alerte : ne laisse partir que `_CHAMPS_GARDES`."""
    garde = {k: v for k, v in event.items() if k in _CHAMPS_GARDES}
    tags = event.get("tags") or {}
    garde["tags"] = {k: v for k, v in tags.items() if k in (TAG, TAG_COURRIEL)}
    return garde


def _suivi(message: str, *, tag: str, valeur: str, empreinte: list[str],
           niveau: str) -> None:
    """Un message au suivi d'erreurs, borné par `_seulement_l_alerte`. Fail-open : le
    suivi d'erreurs indisponible n'annule ni la ligne du journal ni les courriels."""
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.add_event_processor(_seulement_l_alerte)
            scope.set_tag(tag, valeur)
            scope.fingerprint = empreinte
            sentry_sdk.capture_message(message, level=niveau)
    # noqa: SILENT — le suivi d'erreurs indisponible n'annule pas la ligne du journal
    except Exception:
        pass


def alerter(sub: str, role: str, changement: str, quand: Optional[str] = None) -> None:
    """L'alerte d'un compte d'administration dont les facteurs viennent de changer.
    `changement` : l'événement Logto, ou `REMISE_A_ZERO` ; `quand` : son horodatage
    (`createdAt` de la livraison), à défaut l'instant présent. Ne lève pas."""
    quand = quand or datetime.now(timezone.utc).isoformat()
    message = ("double authentification changée sur un compte admin plateforme : "
               f"{sub} ({role}) — {changement}")
    logger.warning(message)
    _suivi(message, tag=TAG, valeur=changement, empreinte=[TAG, sub, quand],
           niveau="warning")
    _courriels(sub, role, changement, quand)


def _courriels(sub: str, role: str, changement: str, quand: str) -> None:
    """Le titulaire, puis chacun des autres admins de la plateforme. Chaque échec est
    dit (journal ERROR + suivi), aucun ne lève."""
    from .. import alertes_equipe, db, email_templates
    try:
        compte = db.get_user(sub) or {}
        titulaire = (compte.get("email") or "").strip()
        equipe = [a for a in alertes_equipe.destinataires() if a != titulaire]
    except Exception as e:  # noqa: SILENT — dit par `_echec` (journal ERROR + suivi), jamais relivré
        _echec(sub, changement, quand, f"destinataires illisibles ({type(e).__name__})")
        return
    envois = [(titulaire, True)] if titulaire else []
    envois += [(adresse, False) for adresse in equipe]
    if not titulaire:
        _echec(sub, changement, quand, "le titulaire n'a pas d'adresse")
    for adresse, est_titulaire in envois:
        try:
            parti = email_templates.send_admin_facteurs_email(
                adresse, compte=titulaire or sub, role=role, changement=changement,
                quand=quand, titulaire=est_titulaire,
                locale=compte.get("locale") if est_titulaire else None)
        except Exception as e:  # noqa: SILENT — dit par `_echec` (journal ERROR + suivi), jamais relivré
            parti, raison = False, type(e).__name__
        else:
            raison = "envoi refusé"
        if not parti:
            _echec(sub, changement, quand,
                   f"{'titulaire' if est_titulaire else 'équipe'} : {raison}")


def _echec(sub: str, changement: str, quand: str, raison: str) -> None:
    message = (f"alerte de double authentification d'un admin plateforme : courriel non "
               f"envoyé pour {sub} ({changement}) — {raison}")
    logger.error(message)
    _suivi(message, tag=TAG_COURRIEL, valeur=changement,
           empreinte=[TAG_COURRIEL, sub, quand], niveau="error")

"""Le retrait SERVI d'une connexion HubSpot — un seul endroit pour les trois capacités
de retrait (membre, org, équipe).

Retirer une connexion faite par « Connect HubSpot » désinstalle l'app du portail
(`auth.hubspot.uninstall_before_clear`), sauf si une autre ligne tient la même
installation. Si la désinstallation échoue sans que HubSpot dise l'installation déjà
partie, le retrait est REFUSÉ (502) et la ligne reste : une app laissée installée
avec ses droits, sans plus personne ici pour la retirer, est pire qu'un retrait à
rejouer.
"""
from __future__ import annotations

from ..auth import hubspot as hubspot_oauth
from ._types import AuthzDenied, DeclaredError

REFUS_DE_DESINSTALLATION = DeclaredError(
    502, "hubspot_uninstall_failed",
    "retrait d'une connexion HubSpot en un clic : la désinstallation de l'app du "
    "portail a échoué (HubSpot injoignable ou refus) ; la connexion est gardée, "
    "réessayer")


def desinstaller_avant_retrait(provider: str, entity_type: str, entity_id: str,
                               account: str) -> None:
    """À appeler AVANT d'effacer la ligne (la désinstallation la lit). Sans effet hors
    HubSpot."""
    if provider != hubspot_oauth.CONNECTOR:
        return
    try:
        hubspot_oauth.uninstall_before_clear(entity_type, entity_id, account)
    except hubspot_oauth.HubSpotUninstallFailed as e:
        raise AuthzDenied(502, "hubspot_uninstall_failed", str(e)) from e

"""Le refus SERVI d'une pose de credential partagée — un seul endroit pour les quatre
capacités qui posent par `credentials_store.preparer_pose` (org, équipe, tenant,
plateforme).

Deux natures de refus remontent du coffre, et elles appellent deux gestes opposés :
- la SAISIE est en cause (champ manquant, forme refusée, clé refusée par le
  fournisseur) : 400, corriger la saisie ;
- la saisie n'a pas pu être VÉRIFIÉE (`VerificationImpossible` : fournisseur
  injoignable, délai, 429, 5xx) : 503, rien à corriger, réessayer. La pose est refusée
  quand même — une clé non vérifiée ne s'écrit pas.

Les refus de la vérification `anthropic` sont DÉCLARÉS par ces capacités
(`REFUS_DE_VERIFICATION`), pour que `/openapi.json` les publie.
"""
from __future__ import annotations

from .. import credentials_store
from ._types import AuthzDenied, DeclaredError

REFUS_DE_VERIFICATION = (
    DeclaredError(400, "anthropic_workspace_required",
                  "clé `anthropic` d'ORGANISATION (créée hors d'un workspace) posée sans "
                  "son workspace : Anthropic refuserait chaque requête — renseigner le "
                  "champ Workspace, ou poser une clé créée dans un workspace"),
    DeclaredError(400, "anthropic_key_rejected",
                  "clé `anthropic` refusée par Anthropic à la pose (invalide, révoquée, "
                  "sans droit, workspace inconnu) — la raison est dans `detail`"),
    DeclaredError(503, "anthropic_unreachable",
                  "Anthropic n'a pas pu vérifier la clé `anthropic` (réseau, délai, 429, "
                  "5xx) : elle n'est pas posée — réessayer"),
)


def refus_de_pose(e: ValueError) -> AuthzDenied:
    """Le refus d'une pose que `preparer_pose` a refusée."""
    if isinstance(e, credentials_store.VerificationImpossible):
        return AuthzDenied(503, e.code, e.message)
    # Refus NOMMÉ quand la validation en porte un (#449) : « champ(s) requis vide(s) :
    # Nom du header » vaut mieux qu'un « incomplet » à deviner.
    return AuthzDenied(400, getattr(e, "code", str(e)),
                       getattr(e, "message", "Credential incomplet ou vide."))

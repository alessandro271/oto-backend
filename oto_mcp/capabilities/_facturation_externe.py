"""La facturation est tenue ailleurs : le refus `billing_moved` (#1097).

Depuis la coupure du cœur, oto-commerce tient la facturation et pose SEUL les droits
déclarés (`org_entitlements`) par l'API de service (`capabilities/service_commerce.py`).
Le cœur n'en écrit plus aucun. Un geste qui vendait, offrait ou déclarait un droit ici
n'a donc plus d'effet possible — le laisser répondre `ok` serait mentir : il écrirait un
abonnement, un contrat ou un don que rien ne transforme en droit.

Ces gestes REFUSENT, nommément, en 409 `billing_moved` : une seule fabrique, pour que le
code, le statut et la phrase ne divergent pas d'une surface à l'autre.
"""
from __future__ import annotations

from ._types import AuthzDenied, DeclaredError

_OU = ("la facturation est tenue par le service de facturation, oto-commerce : vendre "
       "un abonnement, offrir un droit, poser ou clore un contrat se fait là.")


def refus(geste: str, suite: str = "") -> AuthzDenied:
    """Le refus d'un geste de facturation du cœur, qui dit où il se fait désormais."""
    return AuthzDenied(409, "billing_moved", f"{geste} : {_OU}{suite}")


QUAND = ("La facturation est tenue par le service de facturation (oto-commerce) : ce "
         "geste se fait là.")


def declaration(quand: str = QUAND) -> DeclaredError:
    """La déclaration publiée du refus, pour chaque capacité qui le lève. Par défaut, la
    phrase commune à tous les gestes de facturation ; une capacité qui ne refuse que
    dans un cas (une clé du catalogue) dit lequel."""
    return DeclaredError(409, "billing_moved", quand)

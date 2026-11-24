"""L'org visée par l'identité de SERVICE : son existence se vérifie à la route.

Le service n'a pas d'org (`ResolvedCtx.org_id` est None) : l'org vient TOUJOURS du
chemin, et aucune règle d'appartenance ne prouve qu'elle existe — c'est le handler de
la route sous `/api/service/` qui le vérifie, avant de lire ou d'écrire. Une org
inconnue ou archivée est un refus nommé, jamais une page vide ni une ligne écrite pour
une org qui n'existe pas.
"""
from __future__ import annotations

from .. import org_store
from ._types import AuthzDenied, DeclaredError

ORG_INCONNUE = DeclaredError(404, "unknown_org", "l'org n'existe pas ou est archivée")


def org_ou_404(org_id: int) -> None:
    if not org_store.get_org(org_id) or org_store.is_archived_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue ou archivée.")

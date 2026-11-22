"""Le courriel d'ÉQUIPE des alertes d'agents et de clés : à qui il part.

Les deux alertes qui écrivent à un client (`alerte_agents_bloques.balayer`, l'alerte
d'agents bloqués ; `maintenance.alertes_credential`, l'alerte de clé) écrivent aussi à
l'équipe de la plateforme : un courriel SÉPARÉ, jamais une copie visible du client, qui
reprend le contenu de l'org et la nomme. Le client écrit à ses admins et au titulaire
de l'agent ; l'équipe, elle, apprend ce qui part chez chaque client.

**L'équipe, ce sont les comptes qui ont le rôle plateforme** `admin` ou `super_admin`
de CETTE instance, à l'adresse de leur compte. Le rôle se lit comme pour autoriser un
outil `oto_admin_*` (porte `PLATFORM_ADMIN`) : `users.role`, le compte d'amorçage
`OTO_MCP_ADMIN_SUB` compris, jugé par `access.is_operator_role` — la même source que
celle qui écarte ces comptes des courriels CLIENTS (`alerte_agents_bloques.destinataires`).
Aucun réglage à poser : chaque instance écrit à ses propres admins de plateforme.

Un compte suspendu (`users.suspended_at`) ou d'un tenant désactivé
(`tenant_desactive.etat`) est écarté, comme un compte sans adresse.

**Personne** : la liste est vide, et c'est à l'appelant de le dire — une ligne ERROR
qui nomme l'org, aucun courriel d'équipe, les courriels clients partent normalement
(`signaler_sans_equipe`). Ni repli silencieux, ni refus de démarrage.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def destinataires() -> list[str]:
    """Les adresses des admins de plateforme actifs de l'instance, dans l'ordre de
    création des comptes, chacune une fois. Vide s'il n'y en a aucun. Lu à chaque
    appel : jamais mis en cache."""
    from . import access, tenant_desactive
    from .db import users as db_users
    adresses = []
    for r in db_users.operateurs_de_plateforme(os.environ.get("OTO_MCP_ADMIN_SUB")):
        if not access.is_operator_role(r["sub"], r.get("role")):
            continue
        if tenant_desactive.etat(r["sub"]):
            continue
        adresses.append(r["email"].strip())
    return list(dict.fromkeys(a for a in adresses if a))


def signaler_sans_equipe(alerte: str, org_id: int) -> None:
    """La ligne ERROR d'un courriel d'équipe qui n'a personne à qui partir : aucun
    compte n'a le rôle d'admin ou d'opérateur de plateforme."""
    log.error("%s : aucun admin de plateforme actif sur l'instance — courriel d'équipe "
              "NON envoyé pour l'org %s (les courriels clients sont partis)",
              alerte, int(org_id))


def entete(org_id: int, clients: int) -> str:
    """La ligne qui ouvre le courriel d'équipe : l'org, et combien de personnes de
    l'org ont été écrites (`clients`). Texte brut : l'appelant l'échappe."""
    from . import org_store
    org = org_store.get_org(int(org_id)) or {}
    nom = org.get("name") or "?"
    return (f"Copie pour l'équipe — org « {nom} » (#{int(org_id)}), "
            f"{clients} courriel(s) envoyé(s) dans l'org.")

"""Le courriel d'ÉQUIPE des alertes d'agents et de clés : à qui il part, et le refus
quand personne n'est réglé.

Les deux alertes qui écrivent à un client (`alerte_agents_bloques.balayer`, l'alerte
d'agents bloqués ; `maintenance.alertes_credential`, l'alerte de clé) écrivent aussi à
l'équipe de la plateforme : un courriel SÉPARÉ, jamais une copie visible du client, qui
reprend le contenu de l'org et la nomme. Le client écrit à ses admins et au titulaire
de l'agent ; l'équipe, elle, apprend ce qui part chez chaque client.

**Le réglage** : `OTO_ALERTES_EQUIPE_DESTINATAIRES`, variable d'environnement de l'unité
(le `.env` de l'instance), jamais un réglage en base : c'est une propriété de
l'exploitation de l'instance, comme l'expéditeur, et elle doit être lisible AVANT la
base (`boucles_de_fond.composer` est joué avant `_prepare_database`). Une adresse, ou
plusieurs séparées par des virgules ; les blancs autour de chacune sont ignorés.

**Le refus** (`AlertesEquipeNonReglees`) ne part que là où un courriel va réellement
partir :
- la boucle `alerte_agents_bloques` EFFECTIVEMENT retenue par `boucles_de_fond.composer`
  (production déclarée, interrupteur à 1) : le démarrage refuse ;
- le travail `maintenance alertes-credential` avec son envoi ouvert
  (`OTO_ALERTE_CREDENTIAL`) et hors `--dry-run` : le travail refuse, code de sortie 1.
Une préprod, une instance sans boucle d'alerte, un passage à blanc démarrent sans lui.
"""
from __future__ import annotations

import os
import re

ENV = "OTO_ALERTES_EQUIPE_DESTINATAIRES"

#: Une adresse : un `@`, un point dans le domaine, ni blanc ni séparateur.
_ADRESSE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class AlertesEquipeNonReglees(RuntimeError):
    """Une alerte va écrire à des clients, et le courriel d'équipe n'a pas de
    destinataire valide (`OTO_ALERTES_EQUIPE_DESTINATAIRES`)."""


MESSAGE_ABSENT = (
    f"{ENV} n'est pas posée : une alerte d'agents ou de clés va écrire à des clients, et "
    "l'équipe de la plateforme en reçoit un courriel séparé. Posez une adresse, ou "
    "plusieurs séparées par des virgules, dans l'environnement de l'unité ; ou éteignez "
    "l'alerte (OTO_ALERTE_AGENTS_ENABLED=0, OTO_ALERTE_CREDENTIAL vide).")


def destinataires() -> list[str]:
    """Les adresses de l'équipe, dans l'ordre du réglage, chacune une fois. Lève
    `AlertesEquipeNonReglees` si le réglage est absent, vide, ou porte autre chose
    qu'une adresse. Lu à chaque appel : jamais mis en cache."""
    brut = os.environ.get(ENV, "")
    adresses = [a.strip() for a in brut.split(",")]
    if not any(adresses):
        raise AlertesEquipeNonReglees(MESSAGE_ABSENT)
    for a in adresses:
        if not _ADRESSE.match(a):
            raise AlertesEquipeNonReglees(
                f"{ENV} : « {a} » n'est pas une adresse de courriel (attendu : une "
                "adresse, ou plusieurs séparées par des virgules).")
    return list(dict.fromkeys(adresses))


def exiger() -> None:
    """La garde de démarrage d'une boucle qui écrit à l'équipe : lève sans réglage."""
    destinataires()


def entete(org_id: int, clients: int) -> str:
    """La ligne qui ouvre le courriel d'équipe : l'org, et combien de personnes de
    l'org ont été écrites (`clients`). Texte brut : l'appelant l'échappe."""
    from . import org_store
    org = org_store.get_org(int(org_id)) or {}
    nom = org.get("name") or "?"
    return (f"Copie pour l'équipe — org « {nom} » (#{int(org_id)}), "
            f"{clients} courriel(s) envoyé(s) dans l'org.")

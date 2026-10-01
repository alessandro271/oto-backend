"""Déclaration de registre du connecteur `sharepoint`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# sharepoint : fichiers Microsoft 365 (sites SharePoint, bibliothèques de
# documents, OneDrive) via Microsoft Graph. Le champ `directory_id` est l'annuaire
# Entra du client (son « tenant » Microsoft), sans rapport avec le tenant d'oto. byo_org seul : le credential est une
# app Entra enregistrée par l'org dans SON tenant (client credentials, permissions
# d'APPLICATION consenties par son admin) — il vaut pour l'org entière, pas pour
# une personne. Aucune app Entra chez nous, aucun OAuth à mener. Multi-champs
# (ADR 0011), résolu via `access.resolve_credential_fields`. L'hôte Graph est fixe :
# pas de garde d'egress à poser.
CONNECTOR = _c(
    "sharepoint", ["sharepoint"], auth_modes={"byo_org"}, secret_kind="fields",
    label="SharePoint & OneDrive",
    help="fichiers Microsoft 365 : sites, bibliothèques, OneDrive — chercher, lire, déposer",
    href="https://learn.microsoft.com/graph/api/resources/sharepoint",
    credential_fields=(
        CredentialField(
            "directory_id", "ID d'annuaire (tenant)", secret=False,
            help="Entra ID → Vue d'ensemble → ID de locataire (un GUID), ou le "
                 "domaine `votre-societe.onmicrosoft.com`."),
        CredentialField(
            "client_id", "ID d'application (client)", secret=False,
            help="Entra ID → Inscriptions d'applications → ton app → ID "
                 "d'application (client)."),
        CredentialField(
            "client_secret", "Secret client (valeur)", secret=True,
            help="L'app → Certificats et secrets → Nouveau secret client : colle sa "
                 "VALEUR (montrée une seule fois), pas son ID. Il expire : note la date."),
    ),
)

CATEGORY = "Knowledge"
PUBLISHER = "Microsoft"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Les fichiers Microsoft 365 de ton organisation : chercher un site SharePoint, "
    "parcourir ses bibliothèques de documents ou le OneDrive d'un collaborateur, "
    "lire un document (Word, PDF, Excel…) et en déposer un. Par une app Entra que "
    "ton admin Microsoft enregistre et autorise : ses permissions bornent ce qui "
    "est visible."
)

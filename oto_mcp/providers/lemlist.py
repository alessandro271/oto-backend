"""Déclaration de registre du connecteur `lemlist`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "lemlist", ["lemlist"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Lemlist", help="cold outreach", href="https://app.lemlist.com",
    # Trois modules pour UN connecteur, par lisibilité et non par périmètre :
    # `lemlist` tient la campagne et ses leads, `lemlist_crm` tout le reste
    # (CRM, inbox, désinscriptions, signaux, réglages), `lemlist_lignes` le seul
    # geste qui pousse les lignes d'un tableau PAR RÉFÉRENCE. Le namespace reste
    # `lemlist` partout — c'est lui, pas le nom de fichier, que le gate lit
    # (`namespace_of`).
    modules=("lemlist", "lemlist_crm", "lemlist_lignes"),
)

CATEGORY = "Prospection"
PUBLISHER = "lemlist"
LOGO_DOMAIN = "lemlist.com"

DESCRIPTION = (
    "Les campagnes de cold outreach Lemlist : créer et piloter une campagne, "
    "gérer ses leads, plus tout le reste du compte (CRM natif, inbox, "
    "désinscriptions, signaux, réglages) dans un module séparé."
)

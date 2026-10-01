"""Déclaration de registre du connecteur `wttj`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# wttj : l'ATS de Welcome to the Jungle (ex-Welcome Kit), côté recruteur. Jeton
# Bearer DÉLIVRÉ par WTTJ au titulaire du compte (pas de génération en libre-
# service), byo-only : chaque recruteur pose SA clé.
CONNECTOR = _c(
    "wttj", ["wttj"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Welcome to the Jungle",
    help="ATS Welcome to the Jungle — offres et étapes du pipeline, candidats, "
         "commentaires",
    href="https://developers.welcomekit.co",
)

CATEGORY = "Recrutement"
PUBLISHER = "Welcome to the Jungle"
LOGO_DOMAIN = "welcometothejungle.com"

DESCRIPTION = (
    "Le recrutement suivi dans l'ATS de Welcome to the Jungle : offres et leurs "
    "étapes de pipeline, candidats (ajout, déplacement, archivage), commentaires "
    "et historique des déplacements."
)

"""Déclaration de registre du connecteur `typeform`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# typeform : formulaires en ligne, en LECTURE SEULE — espaces de travail,
# formulaires, définition d'un formulaire, réponses. Aucune écriture, aucun
# webhook. byo (user OU org), résolu via `resolve_credential_fields` :
# un jeton personnel agit au nom de son créateur et porte ses scopes, pas de
# clé plateforme partagée.
# DEUX champs : le jeton, et la région du compte. Typeform a trois hôtes (un
# par data center) et les RÉPONSES d'un compte ne se lisent que dans le sien :
# ailleurs, la liste revient vide sans erreur. La région se DÉCLARE à la pose,
# jeu fermé (`choices`) — jamais une URL libre, donc aucune destination saisie.
CONNECTOR = _c(
    "typeform", ["typeform"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Typeform",
    help="online forms, read only: workspaces, forms and their questions, responses",
    href="https://www.typeform.com",
    credential_fields=(
        CredentialField(
            "key", "Typeform personal access token", secret=True,
            help="Typeform → Account → Personal tokens → Generate a new token "
                 "(`tfp_…`). Scopes: forms:read, responses:read, "
                 "workspaces:read. The token acts as the account that created it."),
        CredentialField(
            "region", "Data center", secret=False, required=False,
            choices=("us", "eu", "eu2"),
            help="Where the account's responses are stored: empty or « us » "
                 "(api.typeform.com), « eu » (api.eu.typeform.com), « eu2 » "
                 "(api.typeform.eu, the newer EU data center). Responses read "
                 "from another data center come back empty."),
    ),
)

CATEGORY = "Métier"
PUBLISHER = "Typeform"
LOGO_DOMAIN = "typeform.com"

DESCRIPTION = (
    "Online forms built with Typeform, read only: workspaces, forms with their "
    "questions and choices, and responses filtered by date, completion or text. "
    "Each person or organization connects its own personal access token, with "
    "the account's data center (US or EU) — no shared platform key."
)

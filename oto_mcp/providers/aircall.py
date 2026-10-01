"""Déclaration de registre du connecteur `aircall`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# aircall : téléphonie cloud, en LECTURE SEULE — appels (enregistrement,
# messagerie vocale), IA conversationnelle d'un appel, utilisateurs, équipes,
# numéros, contacts.
#
# Auth **Basic** à DEUX champs (`api_id` + `api_token`), d'où `secret_kind="fields"`
# et la résolution par `access.resolve_credential_fields`. Une clé se crée dans le
# Dashboard Aircall (Company Settings → API Keys) par un compte admin, et donne
# accès à toute la société : pas de portée plus fine côté Aircall.
#
# `api_id` est déclaré NON secret : il identifie la clé posée sans exposer le
# jeton qui va avec (même partage que `leexi` entre son KEY_ID et son secret).
#
# BYOK strict (`byo_user` + `byo_org`, aucun mode plateforme) : ce sont les appels
# et les enregistrements de la société cliente.
CONNECTOR = _c(
    "aircall", ["aircall"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "api_id", "API ID", secret=False,
            help="Aircall Dashboard → Company Settings → API Keys → Add a new "
                 "API key (compte admin requis)."),
        CredentialField(
            "api_token", "API Token", secret=True,
            help="Montré UNE seule fois à la création de la clé : Aircall ne le "
                 "conserve pas en clair."),
    ),
    label="Aircall",
    help="téléphonie, en lecture : appels, enregistrements, transcriptions et "
         "résumés IA, utilisateurs, numéros, contacts",
    href="https://aircall.io",
)

CATEGORY = "Comms"
PUBLISHER = "Aircall"
LOGO_DOMAIN = "aircall.io"

DESCRIPTION = (
    "Les appels d'une société sur Aircall : journal, recherche, enregistrements "
    "et messageries vocales, et — avec l'offre IA d'Aircall — transcription, "
    "résumé, sujets, sentiment et actions à mener. Aussi les utilisateurs, "
    "équipes, numéros et contacts partagés. En lecture seulement ; clé d'API "
    "créée par un admin Aircall."
)

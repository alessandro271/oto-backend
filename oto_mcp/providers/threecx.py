"""Déclaration de registre du connecteur `threecx`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# threecx : standard téléphonique 3CX (v20), en lecture : journal d'appels et
# enregistrements. Multi-champs (ADR 0011), byo-only : l'adresse du standard
# (`base_url`, une DESTINATION → garde d'egress dans `tools/threecx.py`) + UN des
# deux accès, choisi par `auth_mode` (`field_discriminator`) — un client API
# (`client_id`/`client_secret`, que 3CX réserve à certaines licences) ou un compte
# utilisateur (`username`/`password`). `when=` rend chaque paire requise dans SON
# mode seulement : la pose refuse une paire incomplète.
AUTH_MODES = ("api_client", "user")

CONNECTOR = _c(
    "threecx", ["threecx"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="3CX", help="téléphonie, en lecture : journal d'appels et enregistrements",
    href="https://www.3cx.com", field_discriminator="auth_mode", credential_fields=(
        CredentialField(
            "base_url", "Adresse du standard", secret=False,
            help="L'adresse https du client web 3CX, ex. https://votre-societe.3cx.fr"),
        CredentialField(
            "auth_mode", "Accès", secret=False, choices=AUTH_MODES,
            help="api_client : un client API de la console d'administration 3CX "
                 "(Intégrations > API). user : un compte 3CX."),
        CredentialField(
            "client_id", "Client ID", secret=False, when=("api_client",),
            help="L'identifiant du client API ; son rôle borne ce qui est visible "
                 "(System Admin pour tout le standard)."),
        CredentialField(
            "client_secret", "Clé API", secret=True, when=("api_client",)),
        CredentialField(
            "username", "Identifiant du compte 3CX", secret=False, when=("user",),
            help="Les droits de ce compte bornent ce qui est visible ; la double "
                 "authentification doit y être désactivée."),
        CredentialField(
            "password", "Mot de passe du compte 3CX", secret=True, when=("user",),
            whitespace_significant=True),
    ),
)

CATEGORY = "Comms"
PUBLISHER = "3CX"
LOGO_DOMAIN = "3cx.com"

DESCRIPTION = (
    "Le journal d'appels d'un standard 3CX (appelant, appelé, durée, statut) et "
    "l'audio de ses enregistrements. En lecture seulement. Accès par un client API "
    "ou par un compte 3CX : ses droits bornent ce qui est visible."
)

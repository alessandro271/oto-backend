"""Déclaration de registre du connecteur `threecx`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# threecx : standard téléphonique 3CX (v20), en lecture : journal d'appels et
# enregistrements. Multi-champs (ADR 0011), byo-only : l'adresse du standard
# (`base_url`, une DESTINATION → garde d'egress dans `tools/threecx.py`) + UN des
# deux accès, au choix — un client API (`client_id`/`client_secret`, que 3CX
# réserve à certaines licences) ou un compte utilisateur (`username`/`password`).
# D'où `required=False` sur les quatre : la paire complète est exigée par l'outil
# et par la sonde, qui refusent un credential incomplet ou mixte.
CONNECTOR = _c(
    "threecx", ["threecx"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="3CX", help="téléphonie, en lecture : journal d'appels et enregistrements",
    href="https://www.3cx.com", credential_fields=(
        CredentialField(
            "base_url", "Adresse du standard", secret=False,
            help="L'adresse https du client web 3CX, ex. https://votre-societe.3cx.fr"),
        CredentialField(
            "username", "Identifiant d'un compte 3CX", secret=False, required=False,
            help="Avec le mot de passe, si vous n'avez pas de client API. Les droits "
                 "de ce compte bornent ce qui est visible ; la double "
                 "authentification doit y être désactivée."),
        CredentialField(
            "password", "Mot de passe du compte 3CX", secret=True, required=False,
            whitespace_significant=True),
        CredentialField(
            "client_id", "Client ID (client API)", secret=True, required=False,
            help="À la place du compte : un client API créé dans la console "
                 "d'administration 3CX (Intégrations > API)."),
        CredentialField(
            "client_secret", "Client Secret (client API)", secret=True, required=False),
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

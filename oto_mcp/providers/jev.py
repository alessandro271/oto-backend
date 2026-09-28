"""Déclaration de registre du connecteur `jev`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# jev : le modèle de DÉCISION de TypeSafe (System One), servi aujourd'hui par
# OpenRouter. Il rend des réponses typées avec leurs probabilités — jamais du texte,
# jamais un appel d'outil. Il remplace le « je demande au modèle et je parse sa
# réponse » ; il ne remplace pas le modèle qui mène le travail.
#
# ⚠️ Le connecteur porte le nom du MODÈLE, pas celui du passeur. Le jour où Jev se
# prend en direct chez TypeSafe (question d'échelle), seule la base URL du client
# change : le schéma de requête est le même des deux côtés, et la carte du
# catalogue n'a pas à changer de nom. Symétriquement, `openrouter` reste libre pour
# ce qu'il serait alors : un dépôt de clé de MODÈLE (`kind="credential"`), sans
# rapport avec cet outil-ci.
#
# ⚠️ **La clé du TENANT, et elle seule** (décision du 28/09/2026) :
# - pas de clé plateforme (`platform` absent des `auth_modes`) : oto ne paie pas jev ;
#   le tenant qui apporte sa clé répond de la dépense ET de la sous-traitance (l'état
#   part chez OpenRouter puis TypeSafe) ;
# - pas de `byo_user` : une clé de décision n'est pas personnelle ;
# - `byo_org` est déclaré parce que c'est la porte du barreau TENANT
#   (`require_credential("tenant", …)` exige `is_org_shareable`) — c'est LÀ que chaque
#   tenant dépose SA clé, et c'est le seul barreau que les outils acceptent
#   (`tools/jev.py::_client`). Une clé posée au niveau d'une ORG masque celle du tenant
#   dans la cascade : elle est refusée à l'usage, en le disant et en nommant qui la
#   retire. Fermer le dépôt lui-même est un lot à part (un champ déclaré de plus sur
#   `Connector`, contrat lu par oto-dashboard) ;
# - `platform_key_open=False` : pas de free tier. Sans dépôt, le refus est nommé —
#   jamais un repli silencieux sur une clé qui n'est pas celle du demandeur.
CONNECTOR = _c(
    "jev", ["jev"],
    auth_modes={"byo_org"}, keyed=True,
    secret_kind="api_key",
    # MONO-compte, déclaré : un appel tranche avec UNE clé, et deux dépôts sur le
    # même palier n'auraient aucun critère pour se départager.
    cardinality="mono",
    platform_key_open=False,
    label="Jev (décisions)",
    help="décisions typées avec leur probabilité (oui/non, choix, échelle) — clé API "
         "OpenRouter, déposée par un administrateur du tenant, jamais par une org",
    href="https://openrouter.ai/settings/keys",
    # Le champ est NOMMÉ, alors que `secret_kind="api_key"` en dériverait un
    # (« API key ») : la clé attendue ici n'est pas « la clé Jev » — TypeSafe n'en
    # délivre pas — c'est une clé **OpenRouter**, qui passe la requête à TypeSafe et
    # la facture. Un formulaire qui ne le dit pas envoie chercher une clé qui
    # n'existe pas. Le nom interne reste `key` : c'est ce que lit la sonde de
    # connexion et ce que la cascade rend.
    credential_fields=(
        CredentialField("key", "Clé API OpenRouter", secret=True,
                        help="commence par `sk-or-` — créée sur "
                             "openrouter.ai/settings/keys ; c'est elle qui paie les "
                             "décisions, réserve-lui une clé dédiée avec son plafond"),
    ),
)

CATEGORY = "Modèles"
PUBLISHER = "TypeSafe"
LOGO_DOMAIN = "typesafe.ai"

DESCRIPTION = (
    "Jev tranche une question fermée sur un état que tu lui donnes : oui/non avec sa "
    "probabilité, un choix parmi des options, une position sur une échelle. Pas de "
    "texte, pas d'explication — une réponse typée sur laquelle le code branche. "
    "Fait pour trier en lot beaucoup de lignes ou de profils avec la même grille. "
    "L'état part chez un tiers (OpenRouter, puis TypeSafe) : n'y mettre que les champs "
    "utiles au jugement. Servi sur la clé que le tenant dépose."
)

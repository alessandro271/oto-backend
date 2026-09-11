"""Déclaration de registre du connecteur `hellostock`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# hellostock : l'API d'ADMINISTRATION de la marketplace HelloStock (demandes,
# offres, membres, positionnements). Le client vit dans oto-core
# (`oto.tools.hellostock`), les outils dans `tools/hellostock.py` (lectures) et
# `tools/hellostock_ecritures.py` (les trois gestes qui agissent sur la
# marketplace de production).
#
# **byo_user seul, par nature** : le credential est un JETON PERSONNEL créé par
# chaque utilisateur dans son compte HelloStock, qui porte SES droits — et ces
# routes exigent un compte administrateur. Une clé d'org ou de plateforme serait
# un compte nominatif prêté à d'autres, tracé au nom de quelqu'un qui n'a pas agi
# (un envoi de demande est enregistré au nom de l'admin dont c'est le jeton).
#
# ⚠️ ÉCRIT et ENVOIE : `hellostock_demande_send` part en courriel chez des membres
# réels (dry-run PAR DÉFAUT), `hellostock_demande_set_status` et
# `hellostock_offre_update` modifient ce que la marketplace publique affiche.
CONNECTOR = _c(
    "hellostock", ["hellostock"], auth_modes={"byo_user"}, keyed=True,
    secret_kind="api_key",
    modules=("hellostock", "hellostock_ecritures"),
    label="HelloStock administration",
    # `help` part dans la carte des namespaces servie à TOUTES les sessions (instructions
    # MCP), activé ou non : court, et il dit à qui il sert.
    help="demandes, offres, membres et envois de la marketplace — réservé à ses "
         "administrateurs",
    href="https://hellostock.fr",
    credential_fields=(
        CredentialField(
            "key", "Jeton d'API HelloStock (hs_…)", secret=True,
            help="hellostock.fr → Mon espace → Réglages → « Jetons d'API » → créer "
                 "un jeton ; il n'est affiché qu'une fois. Il doit être celui d'un "
                 "compte ADMINISTRATEUR de la marketplace."),
    ),
)

CATEGORY = "Métier"
PUBLISHER = "HelloStock"
LOGO_DOMAIN = "hellostock.fr"

DESCRIPTION = (
    "L'administration de la marketplace HelloStock, depuis ton assistant : passer "
    "en revue les demandes, les offres, les membres et les positionnements, "
    "envoyer une demande aux fournisseurs choisis, écrire le statut et les "
    "mots-clés d'une offre. Réservé aux administrateurs : chacun pose son propre "
    "jeton d'API, et ce qui est fait l'est en son nom."
)

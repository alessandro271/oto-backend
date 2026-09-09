"""Déclaration de registre du connecteur `planity`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# planity : agenda + caisse d'un salon, EN NATIF (kind=tools). Planity n'a pas
# d'API publique : oto rejoue lui-même la connexion à `pro.planity.com` avec
# l'email et le mot de passe posés au coffre (`basic_auth`, palier membre), puis
# parle le protocole WebSocket du Realtime Database de Firebase, les lambdas REST
# de Planity et Algolia. Le cœur vit dans oto-core (`oto.tools.planity`), les
# vingt outils dans `tools/planity*.py` — les noms `planity_*` et leurs schémas
# n'ont PAS bougé (des agents et la fiche les connaissent).
#
# ⚠️ Ce connecteur a été `kind="mount"` jusqu'au 2026-09-09 : les mêmes outils
# étaient servis par un serveur MCP autonome que nous opérions, à qui le backend
# rejouait le `basic_auth` du coffre par requête. Le format stocké et le
# credential ne changent pas d'un iota — ce qui change est qu'il n'y a plus
# d'intermédiaire à opérer, donc plus de passerelle à annoncer dans la fiche.
#
# ⚠️ L'ÉDITEUR, C'EST NOUS (corrigé le 2026-09-02). La fiche a annoncé jusque-là
# « Planity » comme éditeur, avec le logo planity.com : ça se lisait comme une
# intégration officielle, alors que le connecteur est le NÔTRE et qu'il rejoue la
# chaîne d'auth de Planity avec l'email et le mot de passe de l'utilisateur — ce
# que la fiche engage de plus lourd, et qui n'y figurait pas.
CONNECTOR = _c(
    "planity", ["planity"],
    auth_modes={"byo_user"}, secret_kind="basic_auth",
    # Vingt outils : le référentiel, les clientes et l'agenda d'un côté, les
    # chiffres de l'autre. La ligne de partage suit les SOURCES (Realtime
    # Database vs lambdas REST), pas un découpage de confort.
    modules=("planity", "planity_stats"),
    label="Planity",
    help="agenda + caisse Planity (RDV, clients, CA, stats) — oto rejoue ta "
         "connexion Planity avec ton email et ton mot de passe",
    href="https://pro.planity.com",
)

CATEGORY = "Métier"
# Déclaré, pas laissé au défaut : « Otomata » est aussi ce que rend l'ABSENCE de
# constante, et un oubli ne doit pas se confondre avec ce choix-ci.
PUBLISHER = "Otomata"
# Pas le logo de Planity : le connecteur est le nôtre, pas une intégration
# officielle de Planity. Monogramme côté UI.
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "L'agenda et la caisse Planity d'un salon : rendez-vous, clients, chiffre "
    "d'affaires et statistiques. Lecture seule. Planity n'ayant pas d'API "
    "publique, oto rejoue lui-même ta connexion (email + mot de passe) — et "
    "comme le code PIN administrateur de Planity n'est vérifié que dans son "
    "interface, ces identifiants donnent accès à tout ce qu'il protège."
)

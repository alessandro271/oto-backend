"""Déclaration de registre du connecteur `nextmotion`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# nextmotion : gestion de cliniques de médecine esthétique, côté ADMINISTRATIF en
# lecture et en écriture (cliniques, praticiens, agenda, catalogue, ventes, leads,
# appels et messages, statistiques, stock, réglages), plus l'IDENTITÉ du patient.
# keyed api_key (Bearer), BYO seulement : une clé agit au nom de
# l'utilisateur de l'application qui l'a générée, sur les cliniques dont il est
# employé — une clé plateforme n'aurait aucun sens.
#
# ⚠️ Éditeur d'un logiciel qui HÉBERGE DES DONNÉES DE SANTÉ. Le contenu médical
# (antécédents, photos, ordonnances, consentements signés, soins, consultations,
# visites, réponses aux questionnaires) n'est PAS servi ; tout ce qui sort passe par
# une liste blanche, tout ce qui entre aussi, et toute écriture est un aperçu
# (`dry_run`) tant qu'on ne dit pas le contraire. L'identité du patient n'est servie
# que par `nextmotion_patient` ; ailleurs le patient n'est qu'un id — cf.
# `tools/nextmotion.py`.
#
# Sept modules, une seule clé : les outils de `nextmotion.py` et ses frères, montés
# ensemble par `modules`.
CONNECTOR = _c(
    "nextmotion", ["nextmotion"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Nextmotion",
    help="clinique esthétique : agenda, catalogue, devis, factures, paiements, leads, "
         "statistiques, stock, réglages, identité des patients — lecture et écriture "
         "(sans le dossier médical)",
    href="https://www.nextmotion.net",
    modules=("nextmotion", "nextmotion_catalogue", "nextmotion_agenda",
             "nextmotion_ventes", "nextmotion_crm", "nextmotion_patient",
             "nextmotion_analyse"),
)

CATEGORY = "Métier"
PUBLISHER = "Nextmotion"
LOGO_DOMAIN = "nextmotion.net"

DESCRIPTION = (
    "Le côté administratif d'une clinique de médecine esthétique gérée avec "
    "Nextmotion, en lecture et en écriture : cliniques, praticiens, agenda (salles, "
    "appareils, plages, absences, rendez-vous, demandes en ligne, créneaux libres), "
    "catalogue et forfaits, devis, factures, avoirs et paiements, leads, appels et "
    "messages, statistiques de chiffre d'affaires, stock, gabarits et réglages ; "
    "l'identité des patients (fiche, recherche, création, modification) ; patientèle "
    "et occupation des appareils en agrégats. Toute écriture est d'abord un aperçu. "
    "Le dossier médical n'est pas servi : ni antécédents, ni photos, ni ordonnances, "
    "ni soins, ni consultations."
)

#!/bin/bash
# Côté CI : l'environnement GitHub d'une cible EXISTE et EXIGE un relecteur (#967).
#
# Une montée de version de cible est une décision : elle attend l'approbation d'un
# relecteur requis sur l'environnement de la cible. GitHub crée à la volée un
# environnement qu'un job nomme sans qu'il existe — sans aucune protection. Ce contrôle
# tourne AVANT tout job qui nomme l'environnement, et refuse s'il est absent, illisible
# ou sans relecteur requis : jamais de montée qui n'aurait attendu personne.
#
# Ses messages disent « l'environnement de la cible », jamais son nom : les journaux
# d'Actions de ce dépôt sont publics (D5).
#
# usage : protection.sh <dépôt owner/repo> <environnement>     Env : GH_TOKEN
set -euo pipefail
[ "$#" -eq 2 ] || { echo "usage : protection.sh <dépôt> <environnement>" >&2; exit 2; }
DEPOT=$1 ENVIRONNEMENT=$2

if ! reponse=$(gh api "repos/${DEPOT}/environments/${ENVIRONNEMENT}" 2>&1); then
  echo "::error title=Environnement de la cible introuvable::il n'existe pas ou n'a pas pu être lu ($(tr '\n' ' ' <<< "$reponse" | head -c 200)) — rien n'est monté."
  exit 1
fi
relecteurs=$(jq '[.protection_rules[]? | select(.type == "required_reviewers") | .reviewers | length] | add // 0' <<< "$reponse")
if [ "$relecteurs" -lt 1 ]; then
  echo "::error title=Cible sans approbation::l'environnement de la cible n'exige aucun relecteur — poser un relecteur requis (docs/instance-cible.md) avant toute montée."
  exit 1
fi
echo "environnement de la cible : ${relecteurs} relecteur(s) requis"

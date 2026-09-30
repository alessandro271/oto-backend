#!/bin/bash
# Constate, de l'extérieur, qu'un rôle d'une instance cible SERT un tag (#967) : lit
# `GET /api/version` (public, sans jeton) sur l'hôte public du rôle, déclaré dans la
# déclaration de la cible. Un run vert dit qu'un déploiement a été LANCÉ ; seul ce que
# le processus annonce dit ce qui sert (docs/version-servie.md).
#
# Ses messages disent « la préprod de la cible », jamais l'hôte : les journaux d'Actions
# de ce dépôt sont publics (le workflow masque en outre l'hôte et ses URL, D5).
#
# usage : constater.sh <déclaration.json> <rôle> <tag>
set -euo pipefail
[ "$#" -eq 3 ] || { echo "usage : constater.sh <déclaration.json> <rôle> <tag>" >&2; exit 2; }
DECLARATION=$1 ROLE=$2 TAG=$3

hote=$(jq -er --arg r "$ROLE" '.roles[$r].hote_public' "$DECLARATION") \
  || { echo "::error::la déclaration ne porte pas d'hôte public pour le rôle $ROLE"; exit 1; }
for essai in 1 2 3; do
  if servi=$(curl -fsS --max-time 15 "https://${hote}/api/version" | jq -er '.ref'); then
    [ "$servi" = "$TAG" ] && { echo "la $ROLE de la cible sert $TAG"; exit 0; }
    echo "::error title=$ROLE ne sert pas $TAG::la $ROLE de la cible annonce « $servi » (/api/version)."
    exit 1
  fi
  sleep $((essai * 5))
done
echo "::error title=$ROLE injoignable::la $ROLE de la cible n'a pas répondu sur /api/version — rien n'a été constaté."
exit 1

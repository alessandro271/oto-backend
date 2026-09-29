#!/bin/bash
# ============================================================================
# Déploiement d'un rôle d'instance cible (#967) — exécuté depuis l'arbre DU TAG, que la
# porte (deploy/cible/porte.sh) vient d'extraire : l'amorce, la bibliothèque bleu/vert et
# le lanceur sont ceux de la version qu'on monte, jamais une copie posée à la main.
#
#   1. la déclaration dit tout ce qui distingue ce rôle (declaration.py `variables`) ;
#   2. l'amorce remet le rôle à sa forme déclarée — ou le fait naître ;
#   3. la bibliothèque bleu/vert commune installe le tag dans la couleur
#      inactive, la démarre, bascule l'amont Caddy, vérifie le trafic public, vidange ;
#   4. la maintenance du rôle suit l'arbre qui sert désormais.
#
# usage : deployer.sh <déclaration.json> deployer <rôle> <tag>
#         deployer.sh <déclaration.json> retour   <rôle> <tag>   rebascule sur la couleur
#                                                                 inactive (version d'avant)
# Env   : OTO_CIBLE_DEPOT (posée par la porte)
# ============================================================================
# Pas de `set -e` : la bibliothèque bleu/vert gère elle-même ses échecs (rebascule,
# arrêt de la couleur neuve) et ne doit pas être interrompue au premier code non nul.
set -uo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$#" -eq 4 ] || { echo "usage : deployer.sh <déclaration> deployer|retour <rôle> <tag>" >&2; exit 2; }
DECLARATION=$1 ACTION=$2 ROLE=$3 TAG=$4

VARIABLES=$(python3 "$ICI/declaration.py" variables "$DECLARATION" "$ROLE") || exit 1
eval "$VARIABLES"

bash "$ICI/amorcer.sh" "$DECLARATION" "$ROLE" || { echo "déploiement : amorce en échec, rien n'a basculé" >&2; exit 1; }

# shellcheck source=deploy/oto-mcp-bluegreen.sh
. "$ICI/../oto-mcp-bluegreen.sh"

case "$ACTION" in
  deployer) bg_run "$TAG" ;;
  retour)   bg_run --rollback ;;
  *) echo "action inconnue « $ACTION »" >&2; exit 2 ;;
esac

# La maintenance est posée APRÈS la bascule, et n'est jamais un motif d'échec
# du déploiement — une maintenance manquée se rattrape, un service arrêté non. Mais son
# échec se DIT, en tête du journal du run.
if ! bash "$ICI/amorcer.sh" "$DECLARATION" "$ROLE" maintenance; then
  echo "déploiement : ATTENTION — maintenance de ${UNITE} non posée (le service, lui, a basculé)" >&2
fi

#!/bin/bash
# ============================================================================
# Côté CI : frapper à la porte d'une instance cible (#967). Exécuté par le runner
# hébergé de GitHub dans `.github/workflows/deploy-cible.yml` — jamais sur une machine.
#
# SSH par la clé de déploiement (commande forcée vers deploy/cible/porte.sh), à travers
# le tunnel Cloudflare Access de la cible authentifié par jeton de service (le `:22` de
# la machine est fermé au public), clé d'hôte ÉPINGLÉE. La déclaration de la cible part
# sur l'entrée standard ; la commande ne porte que `<action> <rôle> <tag>`.
#
# usage : appeler.sh deployer|retour <rôle> <tag>
# Env (depuis l'environnement GitHub de la cible — voir docs/instance-cible.md) :
#   CIBLE_SSH_HOTE, CIBLE_SSH_UTILISATEUR, CIBLE_SSH_KNOWN_HOSTS    (variables)
#   CIBLE_SSH_CLE, TUNNEL_SERVICE_TOKEN_ID, TUNNEL_SERVICE_TOKEN_SECRET (secrets)
#   CIBLE_DECLARATION                                              (variable, JSON)
# Une valeur absente est un défaut de la déclaration : ROUGE, jamais un vert muet (#823).
# ============================================================================
set -euo pipefail
[ "$#" -eq 3 ] || { echo "usage : appeler.sh deployer|retour <rôle> <tag>" >&2; exit 2; }

manque=""
for v in CIBLE_SSH_HOTE CIBLE_SSH_UTILISATEUR CIBLE_SSH_KNOWN_HOSTS CIBLE_SSH_CLE \
         TUNNEL_SERVICE_TOKEN_ID TUNNEL_SERVICE_TOKEN_SECRET CIBLE_DECLARATION; do
  [ -n "${!v:-}" ] || manque="$manque $v"
done
if [ -n "$manque" ]; then
  echo "::error title=Cible non déclarée::absent(s) de l'environnement GitHub de la cible :$manque — rien n'a été appelé."
  exit 1
fi

if ! command -v cloudflared >/dev/null; then
  # Dépôt APT signé de Cloudflare : la signature est vérifiée par apt, pas une
  # archive « latest » téléchargée sans contrôle.
  sudo install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -qq -o Dir::Etc::sourcelist=sources.list.d/cloudflared.list \
    -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0
  sudo apt-get install -y -qq cloudflared
fi

TRAVAIL=$(mktemp -d)
trap 'rm -rf "$TRAVAIL"' EXIT
umask 077
printf '%s\n' "$CIBLE_SSH_CLE" > "$TRAVAIL/cle"
printf '%s\n' "$CIBLE_SSH_KNOWN_HOSTS" > "$TRAVAIL/hotes"

printf '%s' "$CIBLE_DECLARATION" | ssh -i "$TRAVAIL/cle" \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$TRAVAIL/hotes" \
  -o ProxyCommand='cloudflared access ssh --hostname %h' \
  "${CIBLE_SSH_UTILISATEUR}@${CIBLE_SSH_HOTE}" "$1 $2 $3"

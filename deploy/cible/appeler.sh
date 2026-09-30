#!/bin/bash
# ============================================================================
# Côté CI : frapper à la porte d'une instance cible (#967). Exécuté par le runner
# hébergé de GitHub dans `.github/workflows/deploy-cible.yml` — jamais sur une machine.
#
# SSH par la clé de déploiement (commande forcée vers deploy/cible/porte.sh), clé d'hôte
# ÉPINGLÉE. La déclaration de la cible part sur l'entrée standard ; la commande ne porte
# que `<action> <rôle> <tag>`. Le chemin jusqu'à la machine est un CHOIX explicite
# (`ACCES`), jamais un repli :
#   tunnel  à travers le tunnel Cloudflare Access de la cible, authentifié par jeton de
#           service (`cloudflared`) — le `:22` de la machine est fermé au public ;
#   ssh     directement au `:22` de la machine, en attendant son tunnel : même clé, même
#           commande forcée, même hôte épinglé ; ni `cloudflared` ni jeton.
#
# usage : appeler.sh deployer|retour <rôle> <tag>
# Env (depuis l'environnement GitHub de la cible — voir docs/instance-cible.md) :
#   ACCES                                                          (tunnel | ssh)
#   CIBLE_SSH_HOTE, CIBLE_SSH_UTILISATEUR, CIBLE_SSH_KNOWN_HOSTS    (secrets)
#   CIBLE_SSH_CLE                                                  (secret)
#   TUNNEL_SERVICE_TOKEN_ID, TUNNEL_SERVICE_TOKEN_SECRET           (secrets, mode tunnel)
#   CIBLE_DECLARATION                                              (secret, JSON)
# Une valeur absente est un défaut de la déclaration : ROUGE, jamais un vert muet (#823).
# ============================================================================
set -euo pipefail
[ "$#" -eq 3 ] || { echo "usage : appeler.sh deployer|retour <rôle> <tag>" >&2; exit 2; }

absents() {
  local v manque=""
  for v in "$@"; do [ -n "${!v:-}" ] || manque="$manque $v"; done
  printf '%s' "$manque"
}

case "${ACCES:-}" in
  tunnel|ssh) ;;
  "") echo "::error title=Accès non choisi::ACCES n'est pas posé (tunnel | ssh) — rien n'a été appelé."; exit 1 ;;
  *) echo "::error title=Accès inconnu::ACCES « ${ACCES} » n'est ni tunnel ni ssh — rien n'a été appelé."; exit 1 ;;
esac

manque=$(absents CIBLE_SSH_HOTE CIBLE_SSH_UTILISATEUR CIBLE_SSH_KNOWN_HOSTS CIBLE_SSH_CLE CIBLE_DECLARATION)
if [ -n "$manque" ]; then
  echo "::error title=Cible non déclarée::absent(s) de l'environnement GitHub de la cible :$manque — rien n'a été appelé."
  exit 1
fi

jeton=$(absents TUNNEL_SERVICE_TOKEN_ID TUNNEL_SERVICE_TOKEN_SECRET)
if [ "$ACCES" = tunnel ]; then
  if [ -n "$jeton" ]; then
    echo "::error title=Tunnel sans jeton::accès tunnel choisi, mais absent(s) de l'environnement GitHub de la cible :$jeton (CIBLE_CF_ACCESS_CLIENT_ID / _SECRET) — rien n'a été appelé."
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
  chemin=(-o 'ProxyCommand=cloudflared access ssh --hostname %h')
else
  if [ -n "${TUNNEL_SERVICE_TOKEN_ID:-}${TUNNEL_SERVICE_TOKEN_SECRET:-}" ]; then
    echo "::warning title=Jeton Access inutilisé::accès ssh choisi alors que l'environnement de la cible porte un jeton Access — si son tunnel est prêt, passer en accès tunnel puis fermer le :22 (docs/instance-cible.md)."
  fi
  # Direct au :22, sans mandataire : aucun ssh_config du runner ne s'interpose.
  chemin=(-p 22 -o ProxyCommand=none -o ConnectTimeout=30)
fi

TRAVAIL=$(mktemp -d)
trap 'rm -rf "$TRAVAIL"' EXIT
umask 077
printf '%s\n' "$CIBLE_SSH_CLE" > "$TRAVAIL/cle"
printf '%s\n' "$CIBLE_SSH_KNOWN_HOSTS" > "$TRAVAIL/hotes"

printf '%s' "$CIBLE_DECLARATION" | ssh -i "$TRAVAIL/cle" \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$TRAVAIL/hotes" \
  -o GlobalKnownHostsFile=/dev/null \
  "${chemin[@]}" \
  "${CIBLE_SSH_UTILISATEUR}@${CIBLE_SSH_HOTE}" "$1 $2 $3"

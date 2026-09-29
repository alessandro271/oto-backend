#!/bin/bash
# ============================================================================
# La porte d'une instance cible (#967) — la SEULE chose que la clé de déploiement
# puisse exécuter sur la machine.
#
# Posée une fois par l'opérateur (socle), depuis un tag, à un chemin fixe :
#   install -m 0755 -o root deploy/cible/porte.sh /usr/local/sbin/oto-cible-porte
# puis appelée en commande forcée, à travers le tunnel Cloudflare Access :
#   authorized_keys de l'utilisateur de déploiement (non root) :
#     restrict,command="sudo /usr/local/sbin/oto-cible-porte \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 …
#   sudoers.d : <utilisateur> ALL=(root) NOPASSWD: /usr/local/sbin/oto-cible-porte *
#
# Elle ne sait faire qu'une chose : lire la commande (`deployer|retour <rôle> <tag>`)
# et la déclaration de la cible (JSON, sur l'entrée standard), vérifier que le tag
# existe et appartient au tronc, extraire le `deploy/` DE CE TAG, et lui passer la main
# (`deploy/cible/deployer.sh`). Tout le reste — amorce, bibliothèque bleu/vert, lanceur —
# vient donc du tag déployé, jamais d'une copie posée à la main : la porte est le seul
# fichier de la chaîne qui vit sur la machine, et elle ne change qu'avec son contrat.
#
# Le dépôt est écrit ICI, pas reçu : ni la déclaration ni la commande ne peuvent faire
# exécuter à root le code d'un autre dépôt que le tronc.
# ============================================================================
set -euo pipefail

DEPOT=https://github.com/otomata-tech/oto-backend.git
MIROIR=/var/lib/oto-cible/depot.git
VERROU=/var/lock/oto-cible-porte.lock
TAILLE_MAX=65536
# Ce que la porte extrait du tag : le déploiement, et l'inventaire qui juge la déclaration.
EXTRAIT=(deploy oto_mcp/__init__.py oto_mcp/env_inventory.py oto_mcp/env_secrets.py pyproject.toml)

refus() { echo "porte : REFUS — $*" >&2; exit 1; }

[ "$#" -eq 1 ] || refus "une commande attendue (deployer|retour <rôle> <tag>)"
read -r action role tag reste <<< "$1" || true
[ -z "${reste:-}" ] || refus "commande trop longue : « $1 »"
case "${action:-}" in deployer|retour) ;; *) refus "action inconnue « ${action:-} » (deployer | retour)" ;; esac
case "${role:-}" in preprod|prod) ;; *) refus "rôle inconnu « ${role:-} » (preprod | prod)" ;; esac
[[ "${tag:-}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || refus "tag invalide « ${tag:-} » (vX.Y.Z)"
[ "$(id -u)" = 0 ] || refus "la porte s'exécute par sudo"

exec 8>"$VERROU"
flock -w 900 8 || refus "une autre montée de version tourne déjà sur cette machine"

TRAVAIL=$(mktemp -d /run/oto-cible.XXXXXX)
trap 'rm -rf "$TRAVAIL"' EXIT
umask 077
head -c $((TAILLE_MAX + 1)) > "$TRAVAIL/declaration.json"
taille=$(stat -c %s "$TRAVAIL/declaration.json")
[ "$taille" -gt 0 ] || refus "aucune déclaration reçue sur l'entrée standard"
[ "$taille" -le "$TAILLE_MAX" ] || refus "déclaration de plus de $TAILLE_MAX octets"
umask 022

if [ ! -d "$MIROIR" ]; then
  install -d -m 0755 "$(dirname "$MIROIR")"
  git clone --quiet --mirror "$DEPOT" "$MIROIR"
fi
git -C "$MIROIR" fetch --quiet --prune origin
sha=$(git -C "$MIROIR" rev-parse -q --verify "refs/tags/${tag}^{commit}") \
  || refus "le tag $tag n'existe pas dans le tronc"
git -C "$MIROIR" merge-base --is-ancestor "$sha" refs/heads/main \
  || refus "le tag $tag ($sha) n'est pas sur la branche principale du tronc"

mkdir "$TRAVAIL/tag"
git -C "$MIROIR" archive "$sha" "${EXTRAIT[@]}" | tar -x -C "$TRAVAIL/tag" \
  || refus "le tag $tag ne porte pas le déploiement de cible (antérieur à #967 ?)"
[ -f "$TRAVAIL/tag/deploy/cible/deployer.sh" ] \
  || refus "le tag $tag ne porte pas deploy/cible/deployer.sh"

echo "porte : $action $role $tag ($sha)"
OTO_CIBLE_DEPOT="$DEPOT" bash "$TRAVAIL/tag/deploy/cible/deployer.sh" \
  "$TRAVAIL/declaration.json" "$action" "$role" "$tag"

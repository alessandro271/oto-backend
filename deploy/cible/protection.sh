#!/bin/bash
# Côté CI : une montée de cible n'attend jamais personne sans que quelqu'un l'ait DÉCIDÉE
# (#967). La protection de l'environnement GitHub de la cible est acquise dans deux cas,
# et ce sont les deux seuls :
#   (a) l'environnement EXIGE un relecteur requis — l'approbation est la décision ;
#   (b) l'environnement porte le secret `CIBLE_DECLENCHEURS` (logins GitHub, séparés par
#       des virgules ou des espaces, non vide) et celui qui a lancé le run y figure, à la
#       casse exacte — lancer est la décision. C'est le cas d'un dépôt privé sans
#       relecteurs requis (offre sans Enterprise).
# Hors de ces deux cas : refus nommé. Une liste vide vaut une absence de liste.
#
# Deux temps, parce qu'un secret d'environnement n'est lisible que du job qui NOMME
# l'environnement, et qu'un environnement nommé sans exister serait créé à la volée :
#
#   protection.sh environnement <dépôt owner/repo> <environnement>     Env : GH_TOKEN
#     AVANT tout job qui nomme l'environnement (dépôt du run : l'appelant, quand le
#     workflow est appelé — son jeton doit avoir `actions: read`). L'environnement doit
#     exister ; il exige un relecteur → `relecteurs`, sinon ses déploiements doivent être
#     limités à des branches (sans quoi n'importe quel job de n'importe quelle branche
#     lirait ses secrets) → `declencheurs`. Écrit `protection=<cas>` dans GITHUB_OUTPUT.
#
#   protection.sh declencheurs     Env : PROTECTION, CIBLE_DECLENCHEURS, ACTEUR
#     PREMIER contrôle du job qui nomme l'environnement : en `relecteurs`, l'approbation
#     a eu lieu ; en `declencheurs`, l'acteur doit figurer dans la liste. Tout autre
#     verdict (vide compris) est un refus.
#
# Ses messages disent « l'environnement de la cible », jamais son nom, ni aucun login de
# la liste : les journaux d'Actions de ce dépôt sont publics (D5). Seul l'acteur refusé
# est nommé — il est public sur la page du run.
set -euo pipefail
usage() { echo "usage : protection.sh environnement <dépôt> <environnement> | protection.sh declencheurs" >&2; exit 2; }
refus() { echo "::error title=$1::$2 — rien n'est monté."; exit 1; }

environnement() {
  [ "$#" -eq 2 ] || usage
  local depot=$1 nom=$2 reponse relecteurs branches cas
  [[ "$depot" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
    || refus "Dépôt illisible" "le dépôt du run n'a pas la forme owner/repo"
  if ! reponse=$(gh api "repos/${depot}/environments/${nom}" 2>&1); then
    refus "Environnement de la cible introuvable" "il n'existe pas ou n'a pas pu être lu ($(tr '\n' ' ' <<< "$reponse" | head -c 200)). Appelé depuis un autre dépôt, le job appelant doit accorder « actions: read » à son jeton"
  fi
  relecteurs=$(jq '[.protection_rules[]? | select(.type == "required_reviewers") | .reviewers | length] | add // 0' <<< "$reponse")
  if [ "$relecteurs" -ge 1 ]; then
    cas=relecteurs
    echo "environnement de la cible : ${relecteurs} relecteur(s) requis"
  else
    branches=$(jq -r 'if .deployment_branch_policy == null then "non" else "oui" end' <<< "$reponse")
    [ "$branches" = oui ] \
      || refus "Cible sans protection" "l'environnement de la cible n'exige aucun relecteur, et ses déploiements ne sont pas limités à des branches : ni approbation, ni liste de déclencheurs tenable (docs/instance-cible.md)"
    cas=declencheurs
    echo "environnement de la cible : aucun relecteur requis — la liste de ses déclencheurs (CIBLE_DECLENCHEURS) sera exigée"
  fi
  [ -n "${GITHUB_OUTPUT:-}" ] || refus "Verdict perdu" "GITHUB_OUTPUT n'est pas posé : le verdict de protection ne peut pas être transmis"
  echo "protection=$cas" >> "$GITHUB_OUTPUT"
}

declencheurs() {
  [ "$#" -eq 0 ] || usage
  case "${PROTECTION:-}" in
    relecteurs) echo "montée approuvée par un relecteur requis de l'environnement de la cible"; return ;;
    declencheurs) ;;
    *) refus "Protection non établie" "aucun verdict de protection de l'environnement de la cible (« ${PROTECTION:-} »)" ;;
  esac
  local login liste autorises=() acteur=${ACTEUR:-}
  set -f                      # découper la liste, jamais l'étendre (« * » n'est pas un login)
  liste=${CIBLE_DECLENCHEURS:-}
  for login in ${liste//,/ }; do
    [[ "$login" =~ ^[A-Za-z0-9-]{1,39}$ ]] \
      || refus "Liste des déclencheurs illisible" "CIBLE_DECLENCHEURS doit porter des logins GitHub séparés par des virgules ou des espaces"
    autorises+=("$login")
  done
  [ "${#autorises[@]}" -gt 0 ] \
    || refus "Cible sans protection" "l'environnement de la cible n'exige aucun relecteur et ne déclare aucun déclencheur (CIBLE_DECLENCHEURS absent ou vide)"
  [ -n "$acteur" ] || refus "Déclencheur inconnu" "l'acteur du run n'est pas connu (github.triggering_actor)"
  for login in "${autorises[@]}"; do
    if [ "$login" = "$acteur" ]; then
      echo "déclencheur autorisé par l'environnement de la cible (${#autorises[@]} autorisé(s))"
      return
    fi
  done
  refus "Déclencheur non autorisé" "« ${acteur} » ne figure pas dans la liste des déclencheurs de l'environnement de la cible (CIBLE_DECLENCHEURS, casse exacte)"
}

[ "$#" -ge 1 ] || usage
sous=$1; shift
case "$sous" in
  environnement) environnement "$@" ;;
  declencheurs) declencheurs "$@" ;;
  *) usage ;;
esac

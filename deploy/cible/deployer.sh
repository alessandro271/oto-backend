#!/bin/bash
# ============================================================================
# Déploiement d'un rôle d'instance cible (#967) — exécuté depuis l'arbre DU TAG, que la
# porte (deploy/cible/porte.sh) vient d'extraire : l'amorce, la bibliothèque bleu/vert et
# le lanceur sont ceux de la version qu'on monte, jamais une copie posée à la main.
#
#   1. la déclaration dit tout ce qui distingue ce rôle (declaration.py `variables`) ;
#   2. l'amorce remet le rôle à sa forme déclarée — ou le fait naître ;
#   3. la bibliothèque bleu/vert commune installe le tag dans la couleur
#      inactive, puis — AVANT de la démarrer — la garde des migrations (#1163, #1195) : une
#      base EN RETARD sur une chaîne linéaire du registre DU TAG est migrée depuis cet arbre
#      (`migrer upgrade head`), puis relue ; tout autre écart refuse, en le nommant
#      (docs/instance-cible.md, § Les migrations, jouées par la montée) ;
#   4. elle démarre la couleur, bascule l'amont Caddy, vérifie le trafic public, vidange ;
#   5. la maintenance du rôle suit l'arbre qui sert désormais : ses unités sont rendues et
#      validées AVANT la bascule (un refus n'y bascule rien), posées APRÈS ; un échec de la
#      pose fait échouer le déploiement, en nommant l'arbre que visent encore les unités.
#
# usage : deployer.sh <déclaration.json> deployer <rôle> <tag>
#         deployer.sh <déclaration.json> retour   <rôle> <tag>   rebascule sur la couleur
#                                                                 inactive (version d'avant),
#                                                                 si sa base la connaît
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

# --- le Python du tag (`requires-python`, évalué par deploy/cible/plancher_python.py) et
# --- celui que l'instance choisit (`python` de la déclaration, facultatif). Un choix qui ne
# --- satisfait pas le `requires-python` du tag est refusé ici, avant toute action ; jamais
# --- au retour, qui ne recrée rien.
PLANCHER_PY="$ICI/plancher_python.py"
PYPROJECT_DU_TAG="$ICI/../../pyproject.toml"
PYV=$(python3 "$PLANCHER_PY" plancher "$PYPROJECT_DU_TAG") \
  || { echo "déploiement : REFUS — plancher Python illisible dans le pyproject du tag (motif ci-dessus)" >&2; exit 1; }
PY_VISEE=${PYTHON_CHOISI:-$PYV}
if [ "$ACTION" = deployer ] && [ -n "$PYTHON_CHOISI" ]; then
  python3 "$PLANCHER_PY" satisfait "$PYPROJECT_DU_TAG" "$PYTHON_CHOISI"
  case $? in
    0) ;;
    1) echo "déploiement : REFUS — l'instance choisit Python ${PYTHON_CHOISI} (« python » de la déclaration), que le requires-python de ${TAG} n'admet pas : rien n'est touché" >&2
       exit 1 ;;
    *) exit 1 ;;
  esac
fi

bash "$ICI/amorcer.sh" "$DECLARATION" "$ROLE" || { echo "déploiement : amorce en échec, rien n'a basculé" >&2; exit 1; }

# --- les commandes du lanceur d'un arbre, hors service : mêmes fichiers d'environnement et
# --- même clé d'API que l'unité du rôle, sans le fichier de port (rien n'est servi).
lanceur_de() {
  local arbre=$1
  echo "systemd-run --pipe --wait --quiet --collect -p WorkingDirectory=$arbre" \
       "-p EnvironmentFile=$ETC_ROLE/app.env -p EnvironmentFile=$ETC_ROLE/lanceur.env" \
       "-p LoadCredential=scw:$CLE_SCW $arbre/.venv/bin/python $arbre/deploy/lanceur_secrets.py"
}

# --- la garde des migrations (#1163, #1195), appelée par la bibliothèque bleu/vert dans
# --- l'arbre de la couleur inactive, où le tag vient d'être installé, AVANT son démarrage :
# --- la tête attendue est celle du registre DE CE TAG, jamais du code qui sert. Une base
# --- en retard sur une chaîne linéaire y est migrée (« de → vers » au journal) ; tout autre
# --- écart refuse, et la couleur ne démarre pas.
bg_garde_avant_demarrage() {
  local arbre=$1 lanceur
  lanceur=$(lanceur_de "$arbre")
  $lanceur --script deploy/cible/migrations_a_jour.py --migrer && return 0
  echo "déploiement : REFUS — la base du rôle ${ROLE} ne peut pas porter ${TAG} (motif ci-dessus) : la couleur ne démarre pas." >&2
  echo "  Lire l'état, en root sur la machine :" >&2
  echo "    $lanceur migrer current" >&2
  echo "    $lanceur migrer heads" >&2
  return 1
}

# --- la garde du retour arrière (#1195), dans l'arbre de la couleur PRÉCÉDENTE, avant de la
# --- redémarrer : sa base doit être à la tête que ce code connaît. Une base migrée depuis
# --- par une montée refuse — on corrige vers l'avant, par un nouveau tag.
bg_garde_avant_retour() {
  local arbre=$1 lanceur
  lanceur=$(lanceur_de "$arbre")
  $lanceur --script deploy/cible/migrations_a_jour.py --retour && return 0
  echo "déploiement : REFUS du retour sur ${ROLE} — la base n'est pas à la tête que connaît le code de la couleur précédente (motif ci-dessus)." >&2
  return 1
}

# --- le venv de la couleur qu'on INSTALLE suit le Python du tag et de l'instance. Appelée
# --- par la bibliothèque bleu/vert sur cette seule couleur, après l'attente de vidange et
# --- son arrêt, AVANT `bg_install` (donc avant le `git reset`) — jamais au retour arrière.
# --- L'amorce, elle, ne fait que dire l'écart : elle tourne aussi quand la couleur inactive
# --- est le retour arrière. La règle (docs/instance-cible.md, § Changer de Python) : le
# --- venv est recréé si sa version ne satisfait pas le `requires-python` du tag, ou si elle
# --- diffère du choix de l'instance (`python` de la déclaration) ; la version visée est ce
# --- choix, à défaut le plancher. Sinon : rien. Recréé à l'identique de sa naissance
# --- (amorcer.sh), sur l'interpréteur géré par uv que l'amorce vient de poser, jamais le
# --- Python du système ; `uv sync --frozen` le remplit ensuite. Refus, sans rien toucher,
# --- si la couleur sert ou pourrait servir : son unité tourne, ou le pointeur est
# --- illisible ou la désigne. L'ancien venv est GARDÉ jusqu'à ce que l'installation
# --- réussisse (`bg_apres_installation`) : en cas d'échec, la couleur redevient ce
# --- qu'elle était — code et venv ensemble —, donc un retour arrière valable.
bg_avant_installation() {
  local couleur=$1 arbre=$2 version mm actif code majeur mineur
  AVANT_PLANCHER_ARBRE="" AVANT_PLANCHER_TETE="" AVANT_PLANCHER_VERSION=""
  if [ -x "$arbre/.venv/bin/python" ]; then
    version=$("$arbre/.venv/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) \
      || version="illisible"
  else
    version="absent"
  fi
  mm=$version
  case "$version" in
    absent|illisible) ;;
    *)
      IFS=. read -r majeur mineur _ <<< "$version"; mm="${majeur}.${mineur}"
      python3 "$PLANCHER_PY" satisfait "$PYPROJECT_DU_TAG" "$version"; code=$?
      [ "$code" -le 1 ] || return 1
      if [ "$code" = 0 ] && { [ -z "$PYTHON_CHOISI" ] || [ "$mm" = "$PYTHON_CHOISI" ]; }; then
        return 0
      fi ;;
  esac
  # Le venv change, et seulement alors : les interpréteurs gérés du rôle (posés par
  # l'amorce), et eux seuls — pour le venv recréé ici, et pour le `uv sync` de
  # l'installation qui suit (même shell), s'il devait en recréer un. Sinon,
  # l'environnement de l'installation reste celui d'avant.
  export UV_PYTHON_INSTALL_DIR="$PYTHONS" UV_PYTHON_PREFERENCE=only-managed
  actif=$(bg_active) || { echo "déploiement : REFUS — pointeur de couleur illisible, le venv de ${arbre} n'est pas touché" >&2; return 1; }
  if [ "$actif" = "$couleur" ]; then
    echo "déploiement : REFUS — ${couleur} est la couleur en service, son venv (Python ${mm}) n'est pas touché" >&2
    return 1
  fi
  if systemctl is-active --quiet "${BG_UNIT}@${couleur}"; then
    echo "déploiement : REFUS — ${BG_UNIT}@${couleur} tourne encore, son venv (Python ${mm}) n'est pas touché" >&2
    return 1
  fi
  rm -rf "$arbre/.venv.avant-plancher"
  if [ -e "$arbre/.venv" ]; then
    AVANT_PLANCHER_TETE=$(git -C "$arbre" rev-parse HEAD) \
      || { echo "déploiement : REFUS — tête de ${arbre} illisible, son venv n'est pas touché" >&2; return 1; }
    mv "$arbre/.venv" "$arbre/.venv.avant-plancher" || return 1
    AVANT_PLANCHER_ARBRE=$arbre AVANT_PLANCHER_VERSION=$mm
  fi
  if ! UV_PYTHON_DOWNLOADS=never uv venv --quiet --seed --python "$PY_VISEE" --python-preference only-managed "$arbre/.venv"; then
    rm -rf "$arbre/.venv"
    [ ! -e "$arbre/.venv.avant-plancher" ] || mv "$arbre/.venv.avant-plancher" "$arbre/.venv"
    AVANT_PLANCHER_ARBRE=""
    echo "déploiement : REFUS — venv Python ${PY_VISEE} non créé dans ${arbre} (l'ancien est remis)" >&2
    return 1
  fi
  bg_log "venv ${mm} → ${PY_VISEE} recréé dans ${arbre} (l'ancien est gardé jusqu'à la fin de l'installation)"
}

# --- après `bg_install` de la couleur dont le venv vient d'être recréé : succès, l'ancien
# --- venv part ; échec (fetch, reset, `uv sync`, bac à sable), la couleur reprend son
# --- code d'avant (`git reset --hard` sur sa tête d'avant, si l'installation l'avait déjà
# --- déplacée) ET son venv d'avant, ensemble : elle reste le retour arrière. Si le code
# --- ne peut pas être remis, le venv d'avant n'est pas remis non plus (code neuf + venv
# --- ancien serait pire) : il reste dans `.venv.avant-plancher`, et le journal dit que
# --- cette couleur n'est PAS un retour valable.
bg_apres_installation() {
  local couleur=$1 arbre=$2 code=$3 tete
  [ -n "$AVANT_PLANCHER_ARBRE" ] && [ "$AVANT_PLANCHER_ARBRE" = "$arbre" ] || return 0
  AVANT_PLANCHER_ARBRE=""
  if [ "$code" = 0 ]; then
    rm -rf "$arbre/.venv.avant-plancher"
    return 0
  fi
  tete=$(git -C "$arbre" rev-parse HEAD 2>/dev/null) || tete=""
  if [ "$tete" != "$AVANT_PLANCHER_TETE" ]; then
    if ! git -C "$arbre" reset --quiet --hard "$AVANT_PLANCHER_TETE"; then
      bg_log "ATTENTION : installation en échec et code d'avant (${AVANT_PLANCHER_TETE}) NON remis dans ${arbre} : ${couleur} n'est PAS un retour arrière valable ; venv d'avant (Python ${AVANT_PLANCHER_VERSION}) laissé dans ${arbre}/.venv.avant-plancher"
      return 1
    fi
  fi
  rm -rf "$arbre/.venv"
  if ! mv "$arbre/.venv.avant-plancher" "$arbre/.venv"; then
    bg_log "ATTENTION : installation en échec et venv d'avant NON remis dans ${arbre} : ${couleur} n'est PAS un retour arrière valable"
    return 1
  fi
  bg_log "installation en échec : ${couleur} remise à son état d'avant (code ${AVANT_PLANCHER_TETE}, venv Python ${AVANT_PLANCHER_VERSION}) — elle reste le retour arrière"
  return 1
}

# --- le bac à sable des fonctions : l'exécution de contrôle de l'installation passe par
# --- l'isolement que l'unité du rôle déclare (`instance@.service` : `bwrap`, l'unité n'a
# --- aucun privilège). Sans cela, elle prendrait le défaut `systemd`, qui n'est pas celui
# --- du service.
export OTO_FUNCTIONS_ISOLATION=bwrap

# shellcheck source=deploy/oto-mcp-bluegreen.sh
. "$ICI/../oto-mcp-bluegreen.sh"

# --- les unités de maintenance et d'archive du journal suivent la couleur qui va servir —
# --- celle que la montée installe, ou celle que le retour rebascule : l'inactive, dans les
# --- deux cas. Rendues et validées ICI, avant toute bascule ; un refus arrête tout.
CIBLE=$(bg_other) || { echo "déploiement : REFUS — pointeur de couleur illisible ($BG_ACTIVE) : rien n'a basculé" >&2; exit 1; }
if ! bash "$ICI/amorcer.sh" "$DECLARATION" "$ROLE" maintenance-verifier "$CIBLE"; then
  echo "déploiement : REFUS — les unités de maintenance et d'archive du journal ne se rendent pas pour ${CIBLE} (motif ci-dessus) : rien n'a basculé, ${ROLE} reste servi par $(bg_active)" >&2
  exit 1
fi

case "$ACTION" in
  deployer) bg_run "$TAG" ;;
  retour)   bg_run --rollback ;;
  *) echo "action inconnue « $ACTION »" >&2; exit 2 ;;
esac

# --- l'arbre que nomme une unité posée (son WorkingDirectory), avec sa couleur.
arbre_vise() {
  local fichier=$1 arbre
  [ -f "$fichier" ] || { echo "aucun (unité absente)"; return; }
  arbre=$(sed -n 's/^WorkingDirectory=//p' "$fichier" | head -n 1)
  case "$arbre" in
    "${BG_TREE}-blue")  echo "$arbre (blue)" ;;
    "${BG_TREE}-green") echo "$arbre (green)" ;;
    *) echo "${arbre:-illisible}" ;;
  esac
}

# La maintenance est posée APRÈS la bascule (montée comme retour). Ses unités ont été
# validées avant ; si la pose échoue quand même (écriture, rechargement, timers), le service
# a basculé mais ses unités peuvent nommer l'ancienne couleur — l'arbre que la montée
# suivante réécrira : le déploiement ÉCHOUE, en disant ce qu'elles visent et comment corriger.
if ! bash "$ICI/amorcer.sh" "$DECLARATION" "$ROLE" maintenance; then
  servie=$(bg_active) || servie="?"
  {
    echo "déploiement : ÉCHEC — ${ROLE} a basculé et est servi par ${servie} (${BG_TREE}-${servie}), mais ses unités de maintenance et d'archive du journal ne suivent pas (motif ci-dessus)."
    for nom in maintenance journal-archive; do
      echo "  ${UNITE}-${nom}.service vise : $(arbre_vise "/etc/systemd/system/${UNITE}-${nom}.service")"
    done
    echo "  Corriger la cause, puis reposer les unités, en root sur la machine :"
    echo "    bash ${BG_TREE}-${servie}/deploy/cible/amorcer.sh <déclaration.json> ${ROLE} maintenance"
    echo "  Contrôler : systemctl cat ${UNITE}-maintenance.service ${UNITE}-journal-archive.service | grep WorkingDirectory"
  } >&2
  exit 1
fi

#!/usr/bin/env bash
# Le contrôle INVERSE de compatibilité d'une instance cible (#967, #1195) : c'est la cible
# qui juge le tag. Le contrat d'API que CE tag servirait chez elle (dérivé par import, sans
# serveur) est confronté au contrat qu'épingle le consommateur qu'elle déclare
# (`CIBLE_CONSOMMATEUR`). Seule une rupture d'appel ou de lecture rougit
# (`scripts/contrat-front.py`) ; un contrat illisible rougit aussi : pas de verdict, pas
# de montée.
#
# LA source unique de ce contrôle (#1195) : le workflow de montée d'une cible l'appelle
# avant de rien monter, et `.github/workflows/contrat-cible.yml` le lance SEUL, pour un tag,
# sans rien déployer — le déclencheur du propriétaire de la cible juge ainsi un tag avant
# de décider de le monter. Même script, même verdict.
#
# S'exécute à la racine de l'arbre DU TAG (le tronc extrait au tag).
#
# contrat-cible.sh consommateur
#   Env     : CONSOMMATEUR  JSON {nom, depot owner/repo, chemin, cle: bool, chemin_admin?}
#                           sur une ligne, vide = aucun consommateur déclaré ; TAG (pour
#                           les messages). `chemin_admin` (#1196, facultatif) : le contrat
#                           qu'il épingle sur la console `/api/admin/*`, absente du
#                           document public — jugé contre le document admin du tag.
#   Sorties : (GITHUB_OUTPUT) juger=oui|non ; si oui : nom, depot, chemin, chemin_admin
#             (vide si non déclaré), secret
#   Aucun consommateur → le DIT, juger=non. Mal déclaré → rouge. Sinon exige le verrou du
#   tag : le contrat se dérive du jeu que la cible installera (oto-backend#932).
#
# contrat-cible.sh juger <declaration.json>
#   Env     : NOM, DEPOT, CHEMIN, CHEMIN_ADMIN, SECRET (les sorties de `consommateur`),
#             CLE_LECTURE (la clé de lecture du dépôt du consommateur, si SECRET), TAG
#   Prérequis : le jeu du verrou du tag installé (`oto_mcp` importable par `python`).
#   Sortie 0 = compatible ; 1 = le tag casserait le consommateur, ou rien n'a pu être jugé.
#   La clé de lecture est effacée en sortant, quelle que soit l'issue.
set -euo pipefail

consommateur() {
  if [ -z "${CONSOMMATEUR:-}" ]; then
    echo "::notice title=Aucun consommateur déclaré::la cible ne déclare pas de consommateur (CIBLE_CONSOMMATEUR) — aucun contrat n'a été confronté."
    echo "juger=non" >> "$GITHUB_OUTPUT"; return 0
  fi
  if ! jq -e '(keys - ["chemin_admin","cle","chemin","depot","nom"]) == [] and
         (.nom | test("^[A-Za-z0-9._-]+$")) and
         (.depot | test("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")) and
         (.chemin | test("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")) and
         ((.chemin_admin // null) == null or (.chemin_admin | test("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$"))) and
         (.cle | type == "boolean")' <<< "$CONSOMMATEUR" > /dev/null 2>&1; then
    echo "::error title=Consommateur mal déclaré::CIBLE_CONSOMMATEUR doit être {nom, depot owner/repo, chemin relatif, cle: bool, chemin_admin relatif facultatif} — rien n'a été jugé."
    return 1
  fi
  [ -f uv.lock ] || { echo "::error title=Tag sans verrou::$TAG ne porte pas de uv.lock (tag antérieur à #932) — la cible le refuserait, rien n'est jugé."; return 1; }
  {
    echo "juger=oui"
    echo "nom=$(jq -r .nom <<< "$CONSOMMATEUR")"
    echo "depot=$(jq -r .depot <<< "$CONSOMMATEUR")"
    echo "chemin=$(jq -r .chemin <<< "$CONSOMMATEUR")"
    echo "chemin_admin=$(jq -r '.chemin_admin // ""' <<< "$CONSOMMATEUR")"
    echo "secret=$(jq -r 'if .cle then "CIBLE_CONSOMMATEUR_CLE" else "" end' <<< "$CONSOMMATEUR")"
  } >> "$GITHUB_OUTPUT"
}

juger() {
  local declaration="$1"
  JUGE="$(mktemp -d)"
  TRAVAIL=""
  TRAVAIL_ADMIN=""
  trap 'rm -rf "$JUGE" ${TRAVAIL:+"$TRAVAIL"} ${TRAVAIL_ADMIN:+"$TRAVAIL_ADMIN"}' EXIT
  trap 'exit 1' INT TERM

  # Le contrat que CE tag servirait CHEZ LA CIBLE : la facturation n'y est exposée que si
  # sa déclaration l'allume (sinon le défaut de l'inventaire, qui l'éteint).
  python - "$declaration" <<'PY' > "$JUGE/facturation"
import json, sys
from oto_mcp import env_inventory as inv
roles = json.load(open(sys.argv[1]))["roles"]
valeurs = {r["env"].get("OTO_BILLING_ENABLED", inv.par_nom()["OTO_BILLING_ENABLED"].defaut)
           for r in roles.values()}
if len(valeurs) != 1:
    sys.exit("les rôles de la cible divergent sur OTO_BILLING_ENABLED — contrat ambigu")
print(valeurs.pop())
PY
  OTO_BILLING_ENABLED="$(cat "$JUGE/facturation")" python - <<'PY' > "$JUGE/openapi-du-tag.json"
import json
from oto_mcp import openapi, server
print(json.dumps(openapi.build(server.routes_rest(object()))))
PY
  echo "contrat du tag : $(wc -c < "$JUGE/openapi-du-tag.json") octets"

  # Le contrat épinglé, par l'outil commun (`travail`, `lu`, `contrat` dans ses sorties).
  GITHUB_OUTPUT="$JUGE/lecture" scripts/lire-contrat-consommateur.sh "$NOM" "$DEPOT" "$CHEMIN" "$SECRET"
  TRAVAIL="$(sed -n 's/^travail=//p' "$JUGE/lecture")"
  local lu contrat code=0
  lu="$(sed -n 's/^lu=//p' "$JUGE/lecture")"
  contrat="$(sed -n 's/^contrat=//p' "$JUGE/lecture")"
  if [ "$lu" != oui ]; then
    echo "::error title=Contrat de $NOM non jugé::le contrat épinglé n'a pas pu être lu — la montée de version ne part pas sans verdict."
    return 1
  fi

  python3 scripts/contrat-front.py --consommateur "$NOM" "$contrat" "$JUGE/openapi-du-tag.json" || code=$?
  if [ "$code" = "2" ]; then
    echo "::error title=Contrat de $NOM — confrontation impossible::document illisible, rien n'a été jugé."; return 1
  fi
  if [ "$code" != "0" ]; then
    echo "::error title=$TAG casserait $NOM::une opération qu'il épingle disparaît, change d'entrée, ou ne rend plus ce qu'il lit. Ne pas monter la cible à ce tag avant que $NOM ait suivi."; return 1
  fi

  # Le contrat ADMIN (#1196), si le consommateur en épingle un : la console
  # `/api/admin/*` n'est pas dans le document public, elle l'est dans le document admin
  # du tag — dérivé de même, facturation comprise. Même lecture, même verdict.
  [ -n "${CHEMIN_ADMIN:-}" ] || return 0
  OTO_BILLING_ENABLED="$(cat "$JUGE/facturation")" python - <<'PY' > "$JUGE/openapi-admin-du-tag.json"
import json
from oto_mcp import openapi, server
print(json.dumps(openapi.build(server.routes_rest(object()), admin=True)))
PY
  echo "contrat admin du tag : $(wc -c < "$JUGE/openapi-admin-du-tag.json") octets"
  GITHUB_OUTPUT="$JUGE/lecture-admin" scripts/lire-contrat-consommateur.sh "$NOM" "$DEPOT" "$CHEMIN_ADMIN" "$SECRET"
  TRAVAIL_ADMIN="$(sed -n 's/^travail=//p' "$JUGE/lecture-admin")"
  lu="$(sed -n 's/^lu=//p' "$JUGE/lecture-admin")"
  contrat="$(sed -n 's/^contrat=//p' "$JUGE/lecture-admin")"
  if [ "$lu" != oui ]; then
    echo "::error title=Contrat admin de $NOM non jugé::le contrat admin épinglé n'a pas pu être lu — la montée de version ne part pas sans verdict."
    return 1
  fi
  code=0
  python3 scripts/contrat-front.py --consommateur "$NOM" "$contrat" "$JUGE/openapi-admin-du-tag.json" || code=$?
  if [ "$code" = "2" ]; then
    echo "::error title=Contrat admin de $NOM — confrontation impossible::document illisible, rien n'a été jugé."; return 1
  fi
  if [ "$code" != "0" ]; then
    echo "::error title=$TAG casserait la console chez $NOM::une opération /api/admin/* qu'il épingle disparaît, change d'entrée, ou ne rend plus ce qu'il lit. Ne pas monter la cible à ce tag avant que $NOM ait suivi."; return 1
  fi
}

case "${1:-}" in
  consommateur) consommateur ;;
  juger) [ $# -eq 2 ] || { echo "usage : contrat-cible.sh juger <declaration.json>" >&2; exit 2; }; juger "$2" ;;
  *) echo "usage : contrat-cible.sh consommateur | juger <declaration.json>" >&2; exit 2 ;;
esac

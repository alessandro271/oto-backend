#!/bin/bash
# ============================================================================
# Amorce d'une instance cible (#967) — faire naître un rôle (préprod ou prod) sur une
# machine nue, et le remettre à sa forme déclarée à chaque déploiement.
#
# Versionnée avec le tag : `deploy/cible/deployer.sh` l'appelle depuis l'arbre du tag
# qu'il déploie, AVANT la bascule bleu/vert. Idempotente : sur une machine déjà amorcée,
# elle ne fait que réécrire ce qui se dérive de la déclaration (fichiers d'environnement,
# unités, script de vidange) et vérifier le reste. Tant que l'amorce est une suite de
# gestes, chaque instance diverge le jour de sa naissance ; ici, c'est du code.
#
# Ce qu'elle POSE : l'utilisateur de service (système, sans shell, jamais root), le Python
# du plancher du `pyproject` via uv (Ubuntu 24.04 n'embarque que 3.12), les deux arbres
# du rôle (clone du tronc + venv à eux), les fichiers d'environnement du rôle, les
# unités systemd (gabarits de `deploy/cible/`), le pointeur de couleur à la naissance,
# l'amont Caddy initial, le script de vidange.
#
# Ce qu'elle EXIGE et ne pose jamais (le socle de la machine, geste d'opérateur) :
#   - root ; `uv`, `git`, `caddy`, `python3`, `systemctl` présents ;
#   - la clé d'API du Secret Manager dans <CLE_SCW> (0600, root) ;
#   - le Caddyfile qui importe l'amont du rôle (le message dit la ligne à ajouter).
# Chaque manque refuse, en nommant ce qu'il faut poser.
#
# usage : amorcer.sh <déclaration.json> <rôle>             amorce le rôle
#         amorcer.sh <déclaration.json> <rôle> maintenance-verifier <couleur>
#                                                            rend et valide, sans rien poser,
#                                                            les unités de maintenance pour
#                                                            l'arbre de <couleur> (avant bascule)
#         amorcer.sh <déclaration.json> <rôle> maintenance  pose les unités de maintenance
#                                                            sur l'arbre de la couleur active
# Env   : OTO_CIBLE_DEPOT  URL du dépôt du tronc (posée par la porte, deploy/cible/porte.sh)
# ============================================================================
set -euo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="$(dirname "$ICI")"
RACINE_TAG="$(dirname "$DEPLOY")"
DECLARATION="${1:?déclaration attendue}"
ROLE="${2:?rôle attendu (preprod | prod)}"
ETAPE="${3:-socle}"

amorce_log() { echo "[amorce $(date +%H:%M:%S)] $*"; }
refus() { echo "amorce : REFUS — $*" >&2; exit 1; }

# Une déclaration refusée arrête tout ici, en nommant chaque écart (stderr).
VARIABLES=$(python3 "$ICI/declaration.py" variables "$DECLARATION" "$ROLE") || exit 1
eval "$VARIABLES"

# --- rend un gabarit de deploy/cible/ dans <fichier> : chaque @CLE@ remplacée, aucune laissée.
rendre_dans() {
  local gabarit=$1 fichier=$2 arbre_actif=${3:-}
  [ -r "$gabarit" ] || refus "gabarit $gabarit absent du tag"
  if ! sed -e "s|@UNITE@|${UNITE}|g" -e "s|@UTILISATEUR@|${UTILISATEUR}|g" \
      -e "s|@ARBRE@|${BG_TREE}|g" -e "s|@ETC@|${ETC_ROLE}|g" \
      -e "s|@CLE_SCW@|${CLE_SCW}|g" -e "s|@ARBRE_ACTIF@|${arbre_actif}|g" \
      -e "s|@ARRET_MAX@|$((BG_DRAIN_MAX + 30))|g" \
      "$gabarit" > "$fichier"; then
    rm -f "$fichier"; refus "gabarit $gabarit : rendu en échec"
  fi
  local restes
  restes=$(grep -o '@[A-Z_]*@' "$fichier" | sort -u | tr '\n' ' ') || true
  if [ -n "$restes" ]; then
    rm -f "$fichier"; refus "gabarit $gabarit : clé non rendue (${restes% })"
  fi
}

# --- rend un gabarit et le pose à <cible>.
rendre() {
  local gabarit=$1 cible=$2 arbre_actif=${3:-} tmp
  tmp=$(mktemp)
  rendre_dans "$gabarit" "$tmp" "$arbre_actif"
  install -m 0644 "$tmp" "$cible"
  rm -f "$tmp"
}

# --- les timers de maintenance et d'archive du journal vivent au niveau du RÔLE, pas de la
# --- couleur : chaque rôle a SA base, donc chacun a sa maintenance. Leurs services nomment
# --- l'arbre de la couleur qui sert, et doivent la suivre à chaque bascule (montée ou
# --- retour) : sinon ils tournent sur l'arbre que la montée suivante réécrit, sans que
# --- personne le voie. D'où deux étapes, appelées par deploy/cible/deployer.sh :
# ---   `maintenance-verifier <couleur>`, AVANT la bascule : les unités sont rendues et
# ---     validées pour la couleur qui va servir, dans un dossier jetable, sans rien poser —
# ---     un refus ici arrête la montée, rien n'a basculé ;
# ---   `maintenance`, APRÈS la bascule : les mêmes unités, rendues pour la couleur en
# ---     service, sont posées, rechargées, leurs timers activés, puis RELUES — chaque échec
# ---     refuse en nommant le geste (la montée échoue alors, bruyamment).
# --- <nom de l'unité>:<timer du tag>, le service venant du gabarit deploy/cible/<nom>.service.
UNITES_MAINTENANCE=(maintenance:oto-mcp-maintenance journal-archive:oto-journal-archive)

# --- rend les services et copie les timers, pour l'arbre de <couleur>, dans <dossier>.
preparer_maintenance() {
  local couleur=$1 dossier=$2 paire nom timer
  case "$couleur" in blue|green) ;; *) refus "couleur « $couleur » inconnue (blue | green)" ;; esac
  [ -d "${BG_TREE}-${couleur}" ] || refus "arbre ${BG_TREE}-${couleur} absent — l'amorce du rôle n'a pas tourné"
  for paire in "${UNITES_MAINTENANCE[@]}"; do
    nom=${paire%%:*} timer=${paire#*:}
    rendre_dans "$ICI/${nom}.service" "$dossier/${UNITE}-${nom}.service" "${BG_TREE}-${couleur}"
    [ -r "$DEPLOY/${timer}.timer" ] || refus "timer $DEPLOY/${timer}.timer absent du tag"
    cp "$DEPLOY/${timer}.timer" "$dossier/${UNITE}-${nom}.timer" \
      || refus "timer $DEPLOY/${timer}.timer illisible"
  done
}

if [ "$ETAPE" = maintenance-verifier ] || [ "$ETAPE" = maintenance ]; then
  RENDU=$(mktemp -d)
  trap 'rm -rf "$RENDU"' EXIT
fi

if [ "$ETAPE" = maintenance-verifier ]; then
  couleur="${4:?couleur attendue (blue | green)}"
  preparer_maintenance "$couleur" "$RENDU"
  amorce_log "maintenance : unités rendues et valides pour ${BG_TREE}-${couleur}, posées après la bascule"
  exit 0
fi

if [ "$ETAPE" = maintenance ]; then
  actif=$(cat "$BG_ACTIVE") || refus "pointeur $BG_ACTIVE illisible"
  case "$actif" in blue|green) ;; *) refus "pointeur $BG_ACTIVE illisible (« $actif »)" ;; esac
  preparer_maintenance "$actif" "$RENDU"
  for paire in "${UNITES_MAINTENANCE[@]}"; do
    nom=${paire%%:*}
    for f in "${UNITE}-${nom}.service" "${UNITE}-${nom}.timer"; do
      install -m 0644 "$RENDU/$f" "/etc/systemd/system/$f" \
        || refus "écriture de /etc/systemd/system/$f en échec"
    done
  done
  systemctl daemon-reload || refus "systemctl daemon-reload en échec"
  for paire in "${UNITES_MAINTENANCE[@]}"; do
    nom=${paire%%:*}
    systemctl enable --now "${UNITE}-${nom}.timer" >/dev/null \
      || refus "systemctl enable --now ${UNITE}-${nom}.timer en échec"
  done
  # Relire ce qui est posé, pas ce qu'on a cru écrire.
  for paire in "${UNITES_MAINTENANCE[@]}"; do
    nom=${paire%%:*}
    grep -qxF "WorkingDirectory=${BG_TREE}-${actif}" "/etc/systemd/system/${UNITE}-${nom}.service" \
      || refus "/etc/systemd/system/${UNITE}-${nom}.service, relu, ne vise pas ${BG_TREE}-${actif}"
  done
  amorce_log "maintenance : ${UNITE}-maintenance.timer et ${UNITE}-journal-archive.timer sur ${BG_TREE}-${actif}"
  exit 0
fi
[ "$ETAPE" = socle ] || refus "étape inconnue « $ETAPE » (socle | maintenance-verifier | maintenance)"

# --- ce que l'amorce exige et ne pose pas ----------------------------------------
[ "$(id -u)" = 0 ] || refus "l'amorce tourne en root (par la porte, deploy/cible/porte.sh)"
[ -n "${OTO_CIBLE_DEPOT:-}" ] || refus "OTO_CIBLE_DEPOT (dépôt du tronc) non posée — l'amorce passe par la porte"
for outil in uv git caddy python3 systemctl; do
  command -v "$outil" >/dev/null || refus "$outil absent de la machine (socle, docs/instance-cible.md)"
done
[ -s "$CLE_SCW" ] || refus "clé d'API du Secret Manager absente : poser $CLE_SCW (0600, root)"
[ "$(stat -c %a "$CLE_SCW")" = 600 ] || refus "$CLE_SCW doit être en 0600 (lue par systemd seul)"
[ -f "$BG_CADDYFILE" ] || refus "$BG_CADDYFILE absent — Caddy n'est pas installé"

PYV=$(python3 "$ICI/plancher_python.py" plancher "$RACINE_TAG/pyproject.toml") \
  || refus "plancher Python illisible dans le pyproject du tag (motif ci-dessus)"

# --- utilisateur de service : système, sans shell, sans maison — jamais root -----
if ! id -u "$UTILISATEUR" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
          --shell /usr/sbin/nologin "$UTILISATEUR"
  amorce_log "utilisateur de service ${UTILISATEUR} créé"
fi

# --- arbres, Python, fichiers de configuration ------------------------------------
install -d -m 0755 "/opt/${INSTANCE}" "/usr/local/lib/${INSTANCE}"
install -d -m 0700 "$ETC_INSTANCE" "$ETC_ROLE"
python3 "$ICI/declaration.py" ecrire "$DECLARATION" "$ROLE" / >/dev/null
export UV_PYTHON_INSTALL_DIR="$PYTHONS"
uv python install --quiet "$PYV"
# L'interpréteur que l'instance choisit (`python` de la déclaration), en plus du plancher :
# la montée y recrée le venv de la couleur qu'elle installe (deploy/cible/deployer.sh), qui
# refuse, avant cette amorce, un choix que le tag n'admet pas. L'amorce, elle, ne refuse rien.
if [ -n "$PYTHON_CHOISI" ] && [ "$PYTHON_CHOISI" != "$PYV" ]; then
  uv python install --quiet "$PYTHON_CHOISI"
fi

for couleur in blue green; do
  arbre="${BG_TREE}-${couleur}"
  if [ ! -d "$arbre/.git" ]; then
    git clone --quiet "$OTO_CIBLE_DEPOT" "$arbre"
    amorce_log "arbre ${arbre} cloné"
  fi
  origine=$(git -C "$arbre" remote get-url origin)
  [ "$origine" = "$OTO_CIBLE_DEPOT" ] || refus "$arbre suit $origine, pas le tronc ($OTO_CIBLE_DEPOT)"
  if [ ! -x "$arbre/.venv/bin/python" ]; then
    uv venv --quiet --seed --python "$PYV" --python-preference only-managed "$arbre/.venv"
    amorce_log "venv Python ${PYV} créé dans ${arbre}"
  fi
  # Un venv que le `requires-python` du tag n'admet pas se DIT, et c'est tout : l'amorce
  # ne le recrée ni ne le refuse. Elle tourne à chaque montée, à chaque retour arrière et
  # avant la maintenance — à ces moments, la couleur inactive est le RETOUR ARRIÈRE, et son
  # venv est celui de la version qu'elle porte. Refuser ici bloquerait toute montée dès que
  # le plancher change (les deux couleurs ne changent jamais de Python ensemble), et tout
  # retour vers la couleur d'avant. Même règle que la montée, qui recrée le venv de la
  # couleur qu'elle INSTALLE, et d'elle seule (`bg_avant_installation`,
  # deploy/cible/deployer.sh) : un venv qui satisfait le `requires-python` reste tel quel.
  version=$("$arbre/.venv/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) \
    || version="illisible"
  if [ "$version" = illisible ] || ! python3 "$ICI/plancher_python.py" satisfait "$RACINE_TAG/pyproject.toml" "$version"; then
    amorce_log "${arbre}/.venv est en Python ${version}, que le requires-python du tag n'admet pas (plancher ${PYV}) — laissé tel quel ; recréé par la montée qui installera cette couleur"
  fi
done

# --- naissance : la couleur « en service » d'un rôle qui n'a jamais servi est bleue ;
# --- le premier déploiement installera donc la verte. Seul endroit où une couleur se
# --- suppose, et seulement quand rien n'existe.
if [ ! -e "$BG_ACTIVE" ]; then
  echo blue > "$BG_ACTIVE"
  amorce_log "naissance du rôle ${ROLE} : pointeur de couleur posé (${BG_ACTIVE})"
fi

# --- unités, vidange, amont Caddy -------------------------------------------------
rendre "$ICI/instance@.service" "/etc/systemd/system/${UNITE}@.service"
install -m 0755 "$DEPLOY/oto-mcp-drain.sh" "$BG_DRAIN"
systemctl daemon-reload

if [ ! -e "$BG_UPSTREAM" ]; then
  # shellcheck source=deploy/oto-mcp-bluegreen.sh
  . "$DEPLOY/oto-mcp-bluegreen.sh"
  bg_write_upstream "$(bg_active)"
  amorce_log "amont Caddy initial écrit (${BG_UPSTREAM})"
fi
grep -qF "import ${BG_UPSTREAM}" "$BG_CADDYFILE" || refus "$BG_CADDYFILE n'importe pas l'amont du rôle. \
Ajouter en tête : « import ${BG_UPSTREAM} », puis dans le bloc du site ${BG_DOCSHARE_HOST} : \
« import ${BG_SNIPPET}_upstream » (et « import ${BG_SNIPPET}_upstream_docshare » sous /p/d/*) — docs/instance-cible.md"

amorce_log "rôle ${ROLE} de ${INSTANCE} conforme à sa déclaration"

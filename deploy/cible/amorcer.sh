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
#         amorcer.sh <déclaration.json> <rôle> maintenance  pose le timer de maintenance
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

# --- rend un gabarit de deploy/cible/ : chaque @CLE@ remplacée, aucune laissée.
rendre() {
  local gabarit=$1 cible=$2 arbre_actif=${3:-}
  local tmp; tmp=$(mktemp)
  sed -e "s|@UNITE@|${UNITE}|g" -e "s|@UTILISATEUR@|${UTILISATEUR}|g" \
      -e "s|@ARBRE@|${BG_TREE}|g" -e "s|@ETC@|${ETC_ROLE}|g" \
      -e "s|@CLE_SCW@|${CLE_SCW}|g" -e "s|@ARBRE_ACTIF@|${arbre_actif}|g" \
      "$gabarit" > "$tmp"
  if grep -q '@[A-Z_]*@' "$tmp"; then
    rm -f "$tmp"; refus "gabarit $gabarit : clé non rendue"
  fi
  install -m 0644 "$tmp" "$cible"
  rm -f "$tmp"
}

# --- le timer de maintenance vit au niveau du RÔLE, pas de la couleur : posé APRÈS la
# --- bascule, sur l'arbre de la couleur qui sert (comme install_timers de notre prod).
# --- Chaque rôle a SA base, donc chacun a sa maintenance.
if [ "$ETAPE" = maintenance ]; then
  actif=$(cat "$BG_ACTIVE")
  case "$actif" in blue|green) ;; *) refus "pointeur $BG_ACTIVE illisible (« $actif »)" ;; esac
  rendre "$ICI/maintenance.service" "/etc/systemd/system/${UNITE}-maintenance.service" "${BG_TREE}-${actif}"
  install -m 0644 "$DEPLOY/oto-mcp-maintenance.timer" "/etc/systemd/system/${UNITE}-maintenance.timer"
  systemctl daemon-reload
  systemctl enable --now "${UNITE}-maintenance.timer" >/dev/null
  amorce_log "maintenance : ${UNITE}-maintenance.timer sur ${BG_TREE}-${actif}"
  exit 0
fi
[ "$ETAPE" = socle ] || refus "étape inconnue « $ETAPE » (socle | maintenance)"

# --- ce que l'amorce exige et ne pose pas ----------------------------------------
[ "$(id -u)" = 0 ] || refus "l'amorce tourne en root (par la porte, deploy/cible/porte.sh)"
[ -n "${OTO_CIBLE_DEPOT:-}" ] || refus "OTO_CIBLE_DEPOT (dépôt du tronc) non posée — l'amorce passe par la porte"
for outil in uv git caddy python3 systemctl; do
  command -v "$outil" >/dev/null || refus "$outil absent de la machine (socle, docs/instance-cible.md)"
done
[ -s "$CLE_SCW" ] || refus "clé d'API du Secret Manager absente : poser $CLE_SCW (0600, root)"
[ "$(stat -c %a "$CLE_SCW")" = 600 ] || refus "$CLE_SCW doit être en 0600 (lue par systemd seul)"
[ -f "$BG_CADDYFILE" ] || refus "$BG_CADDYFILE absent — Caddy n'est pas installé"

PYV=$(grep -oP '^requires-python\s*=\s*">=\K[0-9]+\.[0-9]+' "$RACINE_TAG/pyproject.toml") \
  || refus "plancher Python introuvable dans le pyproject du tag"

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
  version=$("$arbre/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  [ "$version" = "$PYV" ] || refus "$arbre/.venv est en Python $version, le tag exige $PYV — recréer ce venv"
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

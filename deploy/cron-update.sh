#!/usr/bin/env bash
# Cron del servidor para mantener fresca la web de la bolilla.
# Cada 5 min:
#   1. git pull (para coger cambios de HTML / scripts)
#   2. fetch_fixtures.py + update_scores.py + update_porra.py
#
# Los JSON se quedan locales — nginx los sirve directamente. No se hace push
# de vuelta a GitHub. Si quieres que GitHub Pages siga al dia, deja activo
# el workflow update_scores.yml en el repo (lento pero independiente).

set -euo pipefail

# Directorio del repo: el padre de deploy/
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Cargar API key
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -o allexport
  source .env
  set +o allexport
else
  echo "[warn] no hay .env, FOOTBALL_DATA_API_KEY puede estar vacio"
fi

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }

# Lock para no solapar runs si uno tarda
exec 200>/tmp/bolilla-cron.lock
flock -n 200 || { log "Otro run en curso, salgo"; exit 0; }

# Actualizar codigo (no falla si no hay cambios)
log "git pull"
git pull --ff-only --quiet || log "git pull fallo (sigue con codigo local)"

# Ejecutar los scripts en orden
log "fetch_fixtures.py"
python3 scripts/fetch_fixtures.py
log "update_scores.py"
python3 scripts/update_scores.py
log "update_porra.py"
python3 scripts/update_porra.py

log "ok"

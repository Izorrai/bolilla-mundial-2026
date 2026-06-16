#!/usr/bin/env bash
# Cron del servidor para mantener fresca la web de la bolilla.
# Cada 5 min:
#   1. git pull (para coger cambios de HTML / scripts)
#   2. fetch_fixtures.py + update_scores.py + update_porra.py
#
# Los JSON se quedan locales — nginx los sirve directamente. No se hace push
# de vuelta a GitHub. Si quieres que GitHub Pages siga al dia, deja activo
# el workflow update_scores.yml en el repo (lento pero independiente).

# OJO: NO usar 'set -e' aqui. Si un script python falla (tipico: API
# temporalmente caida), queremos seguir ejecutando los demas y que el
# scoreboard del server se mantenga con los datos que regenere lo que si
# vaya bien. Con set -e, un fallo en fetch_fixtures.py abortaba toda la
# ejecucion DESPUES del 'git reset --hard', dejando el scoreboard.json
# del server pisado por la version vieja de GitHub.
set -uo pipefail

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

# Antes del 'git reset --hard origin/main' guardamos los JSON que el server
# regenera (scoreboard.json, matches.json, fixtures.json, porra_scoreboard.json,
# scorers.json). Estos archivos en GitHub estan obsoletos (no se actualizan
# desde alli desde que el server es la fuente de verdad). Si los scripts python
# fallaran tras el reset, restauramos el snapshot — asi NUNCA volvemos al
# scoreboard viejo de GitHub.
# Limpiar cualquier marca skip-worktree que se hubiera puesto en runs
# anteriores: era una idea que en la practica rompia el git reset --hard
# si los archivos del server tenian cambios locales. La proteccion contra
# la ventana de pisado la conseguimos via escritura atomica en save_json
# (.tmp + rename) + snapshot/restore aqui mismo.
git ls-files -v 2>/dev/null | awk '/^S/ {print $2}' | xargs -r git update-index --no-skip-worktree 2>/dev/null || true

# Snapshot de los JSON que regenera el server antes del reset.
log "git fetch + reset + pull"
SNAP=$(mktemp -d -t bolilla-XXXXXX)
mkdir -p "$SNAP/data/rooms" "$SNAP/data/porra"
for f in data/matches.json data/fixtures.json data/scorers.json data/porra/porra_scoreboard.json; do
  [[ -f "$f" ]] && { mkdir -p "$SNAP/$(dirname "$f")"; cp -p "$f" "$SNAP/$f"; }
done
shopt -s nullglob
for f in data/rooms/*/scoreboard.json; do
  mkdir -p "$SNAP/$(dirname "$f")"
  cp -p "$f" "$SNAP/$f"
done
shopt -u nullglob

git fetch --quiet origin main || log "fetch fallo"
git reset --hard origin/main --quiet || log "reset fallo (sigue con codigo local)"

# Restaurar SOLO si por alguna razon el archivo cambio durante el reset.
# Con skip-worktree esto no deberia pasar, pero defensa en profundidad.
for f in data/matches.json data/fixtures.json data/scorers.json data/porra/porra_scoreboard.json; do
  if [[ -f "$SNAP/$f" ]] && ! cmp -s "$SNAP/$f" "$f"; then
    log "fallback restaurando $f (skip-worktree no actuo)"
    cp -p "$SNAP/$f" "$f"
  fi
done
shopt -s nullglob
for f in "$SNAP"/data/rooms/*/scoreboard.json; do
  rel="${f#$SNAP/}"
  if [[ -f "$rel" ]] && ! cmp -s "$f" "$rel"; then
    log "fallback restaurando $rel"
    cp -p "$f" "$rel"
  fi
done
shopt -u nullglob
rm -rf "$SNAP"

# Ejecutar los scripts en orden. Cada uno encapsula sus propios errores:
# si fetch_fixtures falla porque la API se cayo, update_scores.py
# usara el matches.json cacheado. Si update_scores tambien falla,
# update_porra usa su propio matches cacheado. Importante: los scripts
# python NO deben tirar excepcion al main por errores transitorios de red.
log "fetch_fixtures.py"
python3 scripts/fetch_fixtures.py || log "fetch_fixtures.py salio con error (no fatal)"
log "update_scores.py"
python3 scripts/update_scores.py || log "update_scores.py salio con error (no fatal)"
log "update_porra.py"
python3 scripts/update_porra.py || log "update_porra.py salio con error (no fatal)"
log "generate_feed.py"
python3 scripts/generate_feed.py || log "generate_feed.py salio con error (no fatal)"

log "ok"

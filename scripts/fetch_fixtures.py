#!/usr/bin/env python3
"""
Descarga TODOS los partidos del Mundial 2026 (en directo, finalizados y por jugar)
y los guarda en data/fixtures.json, junto con el top de goleadores en data/scorers.json.

Lo necesita results.html para mostrar partidos en directo, resultados y goleadores,
y el formulario de la porra para mostrar los 72 partidos de fase de grupos y los
emparejamientos de eliminatorias.

Diferencia con update_scores.py:
  - update_scores.py guarda SOLO partidos terminados (matches.json) para calcular puntos.
  - fetch_fixtures.py guarda TODOS los partidos (fixtures.json), incluido marcador
    y estado (en directo / finalizado / programado), para que el front los muestre.

Uso:
  set FOOTBALL_DATA_API_KEY=xxxx
  python scripts/fetch_fixtures.py
"""

import http.client
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# Errores transitorios de red que merecen retry (no son bug nuestro,
# football-data.org cierra la conexion de vez en cuando)
TRANSIENT_NET_ERRORS = (
    URLError,
    http.client.HTTPException,    # incluye RemoteDisconnected
    ConnectionError,
    TimeoutError,
    socket.timeout,
    OSError,                       # red caida, dns, etc.
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_FILE = ROOT / "data" / "fixtures.json"
SCORERS_FILE = ROOT / "data" / "scorers.json"
API_BASE = "https://api.football-data.org/v4"
MATCHES_API = f"{API_BASE}/competitions/WC/matches"
SCORERS_API = f"{API_BASE}/competitions/WC/scorers?limit=10"


def api_get(url, key, retries=4, backoff=3):
    """GET con reintentos: un 429/5xx o RemoteDisconnected/timeout puntual de
    football-data.org no debe tirar toda la ejecucion (y con ella el refresco
    de marcadores). Backoff exponencial: 3s, 6s, 12s, 24s entre intentos."""
    req = Request(url, headers={"X-Auth-Token": key, "Accept": "application/json"})
    for attempt in range(1, retries + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except TRANSIENT_NET_ERRORS as e:
            if attempt == retries:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(f"[warn] intento {attempt}/{retries} fallido ({type(e).__name__}: {e}); reintentando en {wait}s...")
            time.sleep(wait)


def fetch_fixtures(key):
    payload = api_get(MATCHES_API, key)
    raw = payload.get("matches", [])
    fixtures = []
    for m in raw:
        score = m.get("score") or {}
        # IMPORTANTE: igual que en update_scores.py, en partidos con prorroga o
        # penaltis "fullTime" trae el resultado GLOBAL acumulado, mientras que
        # "regularTime" trae el marcador SOLO de los 90 minutos. Usamos
        # regularTime cuando existe y caemos a fullTime para partidos normales
        # (fase de grupos, donde regularTime no aparece).
        regular_time = score.get("regularTime") or {}
        full_time = score.get("fullTime") or {}
        if regular_time.get("home") is not None and regular_time.get("away") is not None:
            home_goals, away_goals = regular_time["home"], regular_time["away"]
        else:
            home_goals, away_goals = full_time.get("home"), full_time.get("away")
        home_team = m.get("homeTeam") or {}
        away_team = m.get("awayTeam") or {}
        fixtures.append({
            "id": m.get("id"),
            "utcDate": m.get("utcDate"),
            "status": m.get("status"),
            "minute": m.get("minute"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
            "home_team": home_team.get("name"),
            "away_team": away_team.get("name"),
            "home_crest": home_team.get("crest"),
            "away_crest": away_team.get("crest"),
            "home_goals": home_goals,
            "away_goals": away_goals,
        })
    return fixtures


def fetch_scorers(key):
    payload = api_get(SCORERS_API, key)
    raw = payload.get("scorers", [])
    scorers = []
    for s in raw:
        scorers.append({
            "player": (s.get("player") or {}).get("name"),
            "team": (s.get("team") or {}).get("name"),
            "nationality": (s.get("player") or {}).get("nationality"),
            "goals": s.get("goals"),
        })
    return scorers


def save_json(path, data):
    """Escritura atomica: write a .tmp + rename, asi nginx nunca sirve JSON a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def main():
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not key:
        print("ERROR: FOOTBALL_DATA_API_KEY no esta en el entorno.")
        sys.exit(1)

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        fixtures = fetch_fixtures(key)
        save_json(FIXTURES_FILE, {
            "_comment": "Calendario completo del Mundial 2026 generado por scripts/fetch_fixtures.py",
            "last_updated": now_iso,
            "fixtures": fixtures,
        })
        by_status = {}
        for f in fixtures:
            by_status[f["status"]] = by_status.get(f["status"], 0) + 1
        print(f"[ok] {len(fixtures)} partidos guardados en {FIXTURES_FILE.relative_to(ROOT)}")
        for status, n in sorted(by_status.items()):
            print(f"  {status}: {n}")
    except TRANSIENT_NET_ERRORS as e:
        print(f"[warn] no se pudieron obtener los partidos tras varios reintentos (se mantiene fixtures.json anterior): {type(e).__name__}: {e}")

    try:
        scorers = fetch_scorers(key)
        save_json(SCORERS_FILE, {
            "_comment": "Top goleadores generado por scripts/fetch_fixtures.py",
            "last_updated": now_iso,
            "scorers": scorers,
        })
        print(f"[ok] {len(scorers)} goleadores guardados en {SCORERS_FILE.relative_to(ROOT)}")
    except TRANSIENT_NET_ERRORS as e:
        print(f"[warn] no se pudieron obtener los goleadores (se mantiene el archivo anterior): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

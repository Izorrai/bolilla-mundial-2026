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
from datetime import datetime, timedelta, timezone
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
MATCH_BY_ID_API = f"{API_BASE}/matches/{{id}}"
SCORERS_API = f"{API_BASE}/competitions/WC/scorers?limit=10"

# Ventana en la que consideramos que un partido esta "vivo o a punto":
# desde 15 min antes del kickoff hasta 3h despues. Estos se refrescan
# individualmente porque el endpoint de coleccion va con caches de hasta ~1h.
LIVE_WINDOW_BEFORE = timedelta(minutes=15)
LIVE_WINDOW_AFTER = timedelta(hours=3)


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


def _infer_live_status(raw_status, utc_date, now):
    """Workaround para bug de football-data.org: a veces el status se queda
    congelado en TIMED/SCHEDULED aunque el partido lleve un rato en juego
    (visto 50min+ con marcador ya actualizado pero status=TIMED).
    Si ha pasado el kickoff y no supera un limite razonable, forzamos IN_PLAY."""
    if raw_status not in ("TIMED", "SCHEDULED"):
        return raw_status, None
    if not utc_date:
        return raw_status, None
    try:
        kickoff = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return raw_status, None
    elapsed_min = (now - kickoff).total_seconds() / 60.0
    # Ventana [5min, 150min] tras kickoff: asumimos IN_PLAY. Antes de 5 min
    # dejamos SCHEDULED (aun no ha empezado o no llevan casi nada). Despues de
    # 150 min ya podria ser FINISHED con delay, no queremos inventar.
    if 5 <= elapsed_min <= 150:
        # minuto aproximado: 1a parte [0-45], descanso "congelado" en 45,
        # 2a parte descuenta los 15 min de descanso
        if elapsed_min <= 45:
            minute = int(elapsed_min)
        elif elapsed_min <= 60:
            minute = 45   # descanso
        else:
            minute = int(elapsed_min - 15)
        minute = min(minute, 90)
        return "IN_PLAY", minute
    return raw_status, None


def _map_match(m):
    """Convierte un match crudo de football-data.org al formato que usamos en fixtures.json."""
    score = m.get("score") or {}
    # IMPORTANTE: igual que en update_scores.py, en partidos con prorroga o
    # penaltis "fullTime" trae el resultado GLOBAL acumulado, mientras que
    # "regularTime" trae el marcador SOLO de los 90 minutos. Usamos
    # regularTime cuando existe y caemos a fullTime para partidos normales
    # (fase de grupos, donde regularTime no aparece).
    regular_time = score.get("regularTime") or {}
    full_time = score.get("fullTime") or {}
    extra_time = score.get("extraTime") or {}
    if regular_time.get("home") is not None and regular_time.get("away") is not None:
        home_goals, away_goals = regular_time["home"], regular_time["away"]
    elif extra_time.get("home") is not None and extra_time.get("away") is not None and full_time.get("home") is not None:
        # EXTRA_TIME sin penaltis: fullTime acumula 90' + prorroga, restamos extraTime
        home_goals = full_time["home"] - extra_time["home"]
        away_goals = full_time["away"] - extra_time["away"]
    else:
        home_goals, away_goals = full_time.get("home"), full_time.get("away")
    home_team = m.get("homeTeam") or {}
    away_team = m.get("awayTeam") or {}
    # Workaround bug football-data.org: si dice TIMED con kickoff en el pasado,
    # forzamos IN_PLAY y calculamos el minuto nosotros.
    raw_status = m.get("status")
    raw_minute = m.get("minute")
    status, inferred_minute = _infer_live_status(raw_status, m.get("utcDate"), datetime.now(timezone.utc))
    minute = raw_minute if raw_minute is not None else inferred_minute
    winner_raw = score.get("winner")  # "HOME_TEAM", "AWAY_TEAM", "DRAW", None
    duration = score.get("duration")  # "REGULAR_TIME", "EXTRA_TIME", "PENALTY_SHOOTOUT"
    if winner_raw == "HOME_TEAM":
        winner = home_team.get("name")
    elif winner_raw == "AWAY_TEAM":
        winner = away_team.get("name")
    else:
        winner = None
    penalties = score.get("penalties") or {}
    return {
        "id": m.get("id"),
        "utcDate": m.get("utcDate"),
        "status": status,
        "minute": minute,
        "stage": m.get("stage"),
        "group": m.get("group"),
        "matchday": m.get("matchday"),
        "home_team": home_team.get("name"),
        "away_team": away_team.get("name"),
        "home_crest": home_team.get("crest"),
        "away_crest": away_team.get("crest"),
        "home_goals": home_goals,
        "away_goals": away_goals,
        "winner": winner,
        "duration": duration,
        "penalties_home": penalties.get("home"),
        "penalties_away": penalties.get("away"),
    }


def fetch_fixtures(key):
    payload = api_get(MATCHES_API, key)
    raw = payload.get("matches", [])
    return [_map_match(m) for m in raw]


def _is_live_window(fx, now):
    """True si el partido esta en la ventana [-15min, +3h] del kickoff.
    Estos son los que hay que refrescar individualmente porque la coleccion
    puede ir con caches de hasta ~1h."""
    utc = fx.get("utcDate")
    if not utc:
        return False
    # FINISHED ya es final, no vale la pena refrescar (ademas update_scores.py
    # cuenta con matches.json monotonico y no queremos revertir un FINISHED a IN_PLAY).
    if fx.get("status") == "FINISHED":
        return False
    try:
        kickoff = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return (kickoff - LIVE_WINDOW_BEFORE) <= now <= (kickoff + LIVE_WINDOW_AFTER)


def refresh_live_matches(fixtures, key):
    """Segunda pasada: pega a /v4/matches/{id} para cada partido en ventana live.
    El endpoint individual es real-time, la coleccion no."""
    now = datetime.now(timezone.utc)
    live = [fx for fx in fixtures if _is_live_window(fx, now)]
    if not live:
        return fixtures, 0
    by_id = {fx["id"]: fx for fx in fixtures}
    refreshed = 0
    for fx in live:
        mid = fx["id"]
        try:
            payload = api_get(MATCH_BY_ID_API.format(id=mid), key, retries=2, backoff=2)
        except TRANSIENT_NET_ERRORS as e:
            print(f"[warn] no se pudo refrescar match {mid} individual ({type(e).__name__}: {e}); se mantiene el de la coleccion")
            continue
        # /matches/{id} devuelve un objeto con la key "match" o directamente el match
        m = payload.get("match") or payload
        if not isinstance(m, dict) or not m.get("id"):
            continue
        by_id[mid] = _map_match(m)
        refreshed += 1
    return list(by_id.values()), refreshed


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
        # Segunda pasada: refresca los partidos "vivos o a punto" con el endpoint
        # individual, que es real-time. La coleccion tiene un cache de hasta ~1h.
        fixtures, n_refreshed = refresh_live_matches(fixtures, key)
        save_json(FIXTURES_FILE, {
            "_comment": "Calendario completo del Mundial 2026 generado por scripts/fetch_fixtures.py",
            "last_updated": now_iso,
            "fixtures": fixtures,
        })
        by_status = {}
        for f in fixtures:
            by_status[f["status"]] = by_status.get(f["status"], 0) + 1
        print(f"[ok] {len(fixtures)} partidos guardados en {FIXTURES_FILE.relative_to(ROOT)} ({n_refreshed} refrescados individualmente)")
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

#!/usr/bin/env python3
"""
Descarga TODOS los partidos del Mundial 2026 (FINISHED y SCHEDULED) y los guarda
en data/fixtures.json. Lo necesita el formulario de la porra para mostrar los
72 partidos de fase de grupos y los emparejamientos de eliminatorias.

Diferencia con update_scores.py:
  - update_scores.py guarda SOLO partidos terminados (matches.json) para calcular puntos.
  - fetch_fixtures.py guarda TODOS los partidos (fixtures.json) para que el front pueda mostrarlos.

Uso:
  set FOOTBALL_DATA_API_KEY=xxxx
  python scripts/fetch_fixtures.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_FILE = ROOT / "data" / "fixtures.json"
API = "https://api.football-data.org/v4/competitions/WC/matches"


def main():
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not key:
        print("ERROR: FOOTBALL_DATA_API_KEY no esta en el entorno.")
        sys.exit(1)
    req = Request(API, headers={"X-Auth-Token": key, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"ERROR HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}")
        sys.exit(1)
    except URLError as e:
        print(f"ERROR red: {e}")
        sys.exit(1)

    raw = payload.get("matches", [])
    fixtures = []
    for m in raw:
        fixtures.append({
            "id": m.get("id"),
            "utcDate": m.get("utcDate"),
            "status": m.get("status"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
            "home_team": (m.get("homeTeam") or {}).get("name"),
            "away_team": (m.get("awayTeam") or {}).get("name"),
        })

    by_stage = {}
    for f in fixtures:
        by_stage.setdefault(f.get("stage") or "?", 0)
        by_stage[f["stage"]] += 1

    FIXTURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FIXTURES_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "_comment": "Calendario completo del Mundial 2026 generado por scripts/fetch_fixtures.py",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "fixtures": fixtures,
        }, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[ok] {len(fixtures)} partidos guardados en {FIXTURES_FILE.relative_to(ROOT)}")
    for stage, n in sorted(by_stage.items()):
        print(f"  {stage}: {n}")


if __name__ == "__main__":
    main()

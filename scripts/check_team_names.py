#!/usr/bin/env python3
"""
Comprueba que todos los fd_name de data/teams.json existen en la lista de equipos
que la API de football-data.org devuelve para la competicion WC.

Uso:
  set FOOTBALL_DATA_API_KEY=xxxx
  python scripts/check_team_names.py

Si hay nombres que no coinciden, los lista para que los corrijas en data/teams.json.
"""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
TEAMS_FILE = ROOT / "data" / "teams.json"
API = "https://api.football-data.org/v4/competitions/WC/teams"


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

    api_teams = payload.get("teams", [])
    api_names = {t["name"]: t["id"] for t in api_teams}
    print(f"API devolvio {len(api_teams)} equipos para WC")

    with open(TEAMS_FILE, "r", encoding="utf-8") as f:
        doc = json.load(f)

    missing = []
    found = []
    for t in doc["teams"]:
        fd = t.get("fd_name")
        if fd in api_names:
            t["fd_id"] = api_names[fd]
            found.append((t["name"], fd, api_names[fd]))
        else:
            missing.append((t["name"], fd))

    print(f"\n[OK] {len(found)} equipos encontrados (fd_id actualizado)")
    for name, fd, fid in found[:5]:
        print(f"  {name:20} -> {fd:25} id={fid}")
    if len(found) > 5:
        print(f"  ... y {len(found) - 5} mas")

    if missing:
        print(f"\n[FALTAN] {len(missing)} equipos no encontrados en API:")
        for name, fd in missing:
            print(f"  {name:20} (busca '{fd}')")
        print("\nNombres disponibles en API (alfabetico):")
        for n in sorted(api_names.keys()):
            print(f"  - {n}")
    else:
        print("\n[OK] Todos los equipos del catalogo se encontraron en la API.")

    # Sobrescribimos teams.json con los fd_id rellenados
    with open(TEAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\nteams.json actualizado con fd_id de los equipos encontrados.")


if __name__ == "__main__":
    main()

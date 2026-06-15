#!/usr/bin/env python3
"""
Motor de puntuacion de la Bolilla Mundial 2026 (multi-sala).

Lee:
  - data/teams.json                              (catalogo compartido de 48 selecciones)
  - data/rooms.json                              (listado de salas)
  - data/rooms/<room_id>/participants.json       (inscripciones de cada sala)

Llama a football-data.org /v4/competitions/WC/matches (una sola vez por ejecucion) y:
  - Guarda los partidos finalizados en data/matches.json
  - Para cada sala, calcula puntos por participante y escribe
    data/rooms/<room_id>/scoreboard.json

Reglas (recordatorio):
  Solo cuentan los 90 minutos (score.fullTime). Sin prorroga ni penaltis (excepto campeon).
  gol 0.5  victoria 3  empate 1  derrota 0
  bonus acumulado por ronda alcanzada: 16avos 5 / octavos 7 / cuartos 10 / semis 15 / final 25 / campeon 50
  extras (pichichi/mvp/portero): 30 c/u si coinciden con tournament_extras (case-insensitive)
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
from urllib.error import HTTPError, URLError

# Errores transitorios de red que merecen retry
TRANSIENT_NET_ERRORS = (
    URLError,
    http.client.HTTPException,    # incluye RemoteDisconnected
    ConnectionError,
    TimeoutError,
    socket.timeout,
    OSError,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEAMS_FILE = DATA / "teams.json"
ROOMS_FILE = DATA / "rooms.json"
MATCHES_FILE = DATA / "matches.json"

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "WC"

STAGE_ORDER = [
    "GROUP_STAGE",
    "LAST_32", "ROUND_OF_32",
    "LAST_16", "ROUND_OF_16",
    "QUARTER_FINALS",
    "SEMI_FINALS",
    "FINAL",
    "WINNER",
]


def fetch_matches(api_key, retries=4, backoff=3):
    """Llama a la API con reintentos. Si falla tras N intentos devuelve None
    para que el llamante se quede con el cache de matches.json."""
    if not api_key:
        print("[warn] FOOTBALL_DATA_API_KEY not set; will use cached matches.json")
        return None
    url = f"{API_BASE}/competitions/{COMPETITION}/matches"
    req = Request(url, headers={"X-Auth-Token": api_key, "Accept": "application/json"})
    for attempt in range(1, retries + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("matches", [])
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            print(f"[error] HTTP {e.code} from football-data: {body}")
            return None
        except TRANSIENT_NET_ERRORS as e:
            if attempt == retries:
                print(f"[error] network tras {retries} reintentos: {type(e).__name__}: {e}")
                return None
            wait = backoff * (2 ** (attempt - 1))
            print(f"[warn] intento {attempt}/{retries} fallido ({type(e).__name__}: {e}); reintento en {wait}s")
            time.sleep(wait)


def normalize_matches(raw_matches):
    keep = []
    for m in raw_matches:
        if m.get("status") != "FINISHED":
            continue
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        if ft.get("home") is None or ft.get("away") is None:
            continue
        keep.append({
            "id": m.get("id"),
            "utcDate": m.get("utcDate"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
            "home_team": (m.get("homeTeam") or {}).get("name"),
            "away_team": (m.get("awayTeam") or {}).get("name"),
            "home_goals_90": ft["home"],
            "away_goals_90": ft["away"],
            "duration": score.get("duration"),
            "winner_overall": score.get("winner"),
        })
    return keep


def build_fd_name_to_team(teams):
    by_name = {}
    for t in teams:
        if t.get("fd_name"):
            by_name[t["fd_name"].lower()] = t
        by_name[t["name"].lower()] = t
    return by_name


def team_match_outcome(team_name, m, fd_lookup):
    home = (m["home_team"] or "").lower()
    away = (m["away_team"] or "").lower()
    t = fd_lookup.get(team_name.lower())
    if not t:
        return None, 0, 0
    target_fd = (t.get("fd_name") or "").lower()
    if home in (target_fd, team_name.lower()):
        gf, ga = m["home_goals_90"], m["away_goals_90"]
    elif away in (target_fd, team_name.lower()):
        gf, ga = m["away_goals_90"], m["home_goals_90"]
    else:
        return None, 0, 0
    if gf > ga:  return "win", gf, ga
    if gf < ga:  return "loss", gf, ga
    return "draw", gf, ga


def stage_reached(team_name, matches, fd_lookup):
    reached = "GROUP_STAGE"
    has_match = False
    for m in matches:
        if team_match_outcome(team_name, m, fd_lookup)[0] is None:
            continue
        has_match = True
        st = m.get("stage")
        if st in STAGE_ORDER and STAGE_ORDER.index(st) > STAGE_ORDER.index(reached):
            reached = st
    return reached if has_match else None


def champion_team(matches):
    finals = [m for m in matches if m.get("stage") == "FINAL"]
    if not finals:
        return None
    final = finals[-1]
    w = final.get("winner_overall")
    if w == "HOME_TEAM": return final["home_team"]
    if w == "AWAY_TEAM": return final["away_team"]
    return None


def team_round_bonus(stage_reached_value):
    bonuses = {
        "GROUP_STAGE":     0,
        "LAST_32":         5,
        "ROUND_OF_32":     5,
        "LAST_16":         5 + 7,
        "ROUND_OF_16":     5 + 7,
        "QUARTER_FINALS":  5 + 7 + 10,
        "SEMI_FINALS":     5 + 7 + 10 + 15,
        "FINAL":           5 + 7 + 10 + 15 + 25,
        "WINNER":          5 + 7 + 10 + 15 + 25 + 50,
    }
    return bonuses.get(stage_reached_value, 0)


def compute_team_stats(team_name, matches, fd_lookup, is_champion):
    goals_for = goals_against = wins = draws = losses = matches_played = 0
    for m in matches:
        outcome, gf, ga = team_match_outcome(team_name, m, fd_lookup)
        if outcome is None:
            continue
        matches_played += 1
        goals_for += gf
        goals_against += ga
        if outcome == "win":   wins += 1
        elif outcome == "draw": draws += 1
        else:                   losses += 1
    stage = stage_reached(team_name, matches, fd_lookup)
    if is_champion:
        stage = "WINNER"
    rb = team_round_bonus(stage)
    pts_goals = 0.5 * goals_for
    pts_results = 3 * wins + 1 * draws
    return {
        "matches_played": matches_played,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "wins": wins, "draws": draws, "losses": losses,
        "stage_reached": stage or "—",
        "round_bonuses": rb,
        "goals_pts": round(pts_goals, 1),
        "results_pts": pts_results,
        "points_generated": round(pts_goals + pts_results + rb, 1),
    }


def compute_extras_points(participant, extras_official):
    if not extras_official:
        return 0
    pts = 0
    eq = lambda a, b: bool(a and b and a.strip().lower() == b.strip().lower())
    if eq(participant.get("pichichi"), extras_official.get("pichichi")):  pts += 30
    if eq(participant.get("mvp"), extras_official.get("mvp")):            pts += 30
    if eq(participant.get("gk"), extras_official.get("best_gk")):         pts += 30
    return pts


def compute_room_scoreboard(room_id, participants_doc, team_stats, teams):
    participants = participants_doc.get("participants", [])
    extras_official = participants_doc.get("tournament_extras") or {}
    ranking = []
    for p in participants:
        name = p.get("name", "—")
        selections = p.get("selections") or {}
        team_points_list = []
        sum_goals = sum_results = sum_rounds = 0.0
        for g in ["A", "B", "C", "D", "E", "F"]:
            for tname in selections.get(g, []) or []:
                s = team_stats.get(tname)
                if not s:
                    team_points_list.append({"name": tname, "group": g, "points": 0, "details": {}})
                    continue
                team_points_list.append({
                    "name": tname, "group": g, "points": s["points_generated"],
                    "details": {
                        "PJ": s["matches_played"], "V": s["wins"], "E": s["draws"], "D": s["losses"],
                        "GF": s["goals_for"], "stage": s["stage_reached"],
                    },
                })
                sum_goals += s["goals_pts"]
                sum_results += s["results_pts"]
                sum_rounds += s["round_bonuses"]
        extras_pts = compute_extras_points(p, extras_official)
        total = round(sum_goals + sum_results + sum_rounds + extras_pts, 1)
        ranking.append({
            "name": name,
            "total": total,
            "breakdown": {
                "goals": round(sum_goals, 1),
                "match_results": round(sum_results, 1),
                "round_bonuses": round(sum_rounds, 1),
                "extras": extras_pts,
            },
            "team_points": team_points_list,
        })
    ranking.sort(key=lambda x: (-x["total"], x["name"].lower()))
    return ranking


# ---------- Rank-change annotations (movimientos divertidos en la clasificacion) ----------

def rankings_equal(a, b):
    """Dos rankings se consideran iguales si tienen las mismas personas con los
    mismos puntos (no nos importa el orden tras el sort, que es determinista)."""
    if len(a) != len(b):
        return False
    da = {e["name"]: e.get("total", 0) for e in a}
    db = {e["name"]: e.get("total", 0) for e in b}
    return da == db


def annotate_changes(new_ranking, prev_ranking):
    """Anade campos por entrada de new_ranking:
      - previous_position (int | None)
      - delta_position (int): positivo si subio, negativo si bajo, 0 si igual
      - delta_total (float): diferencia de puntos vs prev
      - overtook (list[str]): nombres a los que el jugador acaba de adelantar
      - new_in_ranking (bool): si no aparecia antes en prev_ranking
    """
    prev_pos = {e["name"]: i + 1 for i, e in enumerate(prev_ranking)}
    prev_tot = {e["name"]: e.get("total", 0) for e in prev_ranking}
    new_pos = {e["name"]: i + 1 for i, e in enumerate(new_ranking)}
    for i, e in enumerate(new_ranking):
        n = e["name"]
        npos = i + 1
        ppos = prev_pos.get(n)
        if ppos is None:
            e["new_in_ranking"] = True
            e["previous_position"] = None
            e["delta_position"] = 0
            e["delta_total"] = e.get("total", 0)
            e["overtook"] = []
        else:
            e["new_in_ranking"] = False
            e["previous_position"] = ppos
            e["delta_position"] = ppos - npos  # +N = subio N puestos
            e["delta_total"] = round(e.get("total", 0) - prev_tot.get(n, 0), 1)
            overtook = []
            for other in new_ranking:
                on = other["name"]
                if on == n:
                    continue
                op = prev_pos.get(on)
                if op is None:
                    continue
                # estaba delante de mi y ahora esta detras
                if op < ppos and new_pos[on] > npos:
                    overtook.append(on)
            e["overtook"] = overtook
    return new_ranking


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()

    teams_doc = load_json(TEAMS_FILE, {"teams": []})
    teams = teams_doc.get("teams", [])
    fd_lookup = build_fd_name_to_team(teams)

    rooms_doc = load_json(ROOMS_FILE, {"rooms": [{"id": "default", "name": "Default"}]})
    rooms = rooms_doc.get("rooms", [])

    raw = fetch_matches(api_key)
    if raw is None:
        cached = load_json(MATCHES_FILE, {"matches": []})
        matches = cached.get("matches", [])
        print(f"[info] using {len(matches)} cached matches")
    else:
        matches = normalize_matches(raw)
        print(f"[info] fetched {len(raw)} matches, kept {len(matches)} finished")
        save_json(MATCHES_FILE, {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "matches": matches,
        })

    champ = champion_team(matches)
    team_stats = {}
    for t in teams:
        is_champ = bool(champ) and (
            t["name"].lower() == (champ or "").lower() or
            (t.get("fd_name") or "").lower() == (champ or "").lower()
        )
        team_stats[t["name"]] = compute_team_stats(t["name"], matches, fd_lookup, is_champ)

    now_iso = datetime.now(timezone.utc).isoformat()
    for room in rooms:
        room_id = room["id"]
        room_dir = DATA / "rooms" / room_id
        participants_file = room_dir / "participants.json"
        scoreboard_file = room_dir / "scoreboard.json"
        participants_doc = load_json(participants_file, {"participants": [], "tournament_extras": {}})
        prev_scoreboard = load_json(scoreboard_file, {})
        last_persisted_ranking = prev_scoreboard.get("ranking", [])
        sticky_previous = prev_scoreboard.get("previous_ranking", [])

        ranking = compute_room_scoreboard(room_id, participants_doc, team_stats, teams)

        # "Previous" sticky: si el ranking actual coincide con el ultimo persistido,
        # mantenemos como previous el que ya teniamos (asi los deltas no desaparecen
        # entre runs sin cambios). Si el ranking cambio, el persistido pasa a ser previous.
        if rankings_equal(last_persisted_ranking, ranking):
            chosen_prev = sticky_previous
        else:
            chosen_prev = last_persisted_ranking

        annotate_changes(ranking, chosen_prev)

        save_json(scoreboard_file, {
            "last_updated": now_iso,
            "ranking": ranking,
            "previous_ranking": chosen_prev,
            "team_stats": team_stats,
            "champion": champ,
            "room_id": room_id,
            "room_name": room.get("name", room_id),
        })
        print(f"[ok] room '{room_id}': {len(ranking)} participants -> {scoreboard_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

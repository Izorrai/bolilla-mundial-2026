#!/usr/bin/env python3
"""
Motor de puntuacion de la Bolilla Mundial 2026.

Lee:
  - data/teams.json        (catalogo de 51 selecciones con fd_name y precio)
  - data/participants.json (inscripciones + extras oficiales)

Llama a football-data.org /v4/competitions/WC/matches y:
  - Guarda los partidos finalizados en data/matches.json
  - Calcula los puntos por participante segun las reglas de la bolilla
  - Escribe data/scoreboard.json con ranking + breakdown + team_stats

Reglas (recordatorio):
  Solo cuentan los 90 minutos (score.fullTime). Sin prorroga ni penaltis.
  gol 0.5  victoria 3  empate 1  derrota 0
  bonus de ronda alcanzada: 16avos 5  octavos 7  cuartos 10  semis 15  final 25  campeon 50
  extras (pichichi/mvp/portero): 30 c/u si coinciden con tournament_extras (case-insensitive)

Uso (local):
  set FOOTBALL_DATA_API_KEY=xxxx
  python scripts/update_scores.py

Uso (CI):
  github actions cron lo lanza con el secret FOOTBALL_DATA_API_KEY
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEAMS_FILE = DATA / "teams.json"
PARTICIPANTS_FILE = DATA / "participants.json"
MATCHES_FILE = DATA / "matches.json"
SCOREBOARD_FILE = DATA / "scoreboard.json"

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "WC"  # FIFA World Cup

# Mapping football-data stage -> our scoring round bonus.
# Note: World Cup 2026 has 48 teams -> round of 32 added. football-data may use
# either "LAST_16" or "ROUND_OF_16" depending on competition. We map both.
STAGE_BONUS = {
    "GROUP_STAGE":        0,    # group stage advancement does not award by itself; bonus comes from REACHING next stage
    "LAST_32":            5,    # reached round of 32 (after group)
    "ROUND_OF_32":        5,
    "LAST_16":            7,    # reached round of 16
    "ROUND_OF_16":        7,
    "QUARTER_FINALS":    10,
    "SEMI_FINALS":       15,
    "FINAL":             25,
    "WINNER":            50,    # champion (cumulative bonus uses CHAMPION below)
}

# Order of stages from earliest to latest. Used to determine "how far did a team go".
STAGE_ORDER = [
    "GROUP_STAGE",
    "LAST_32", "ROUND_OF_32",
    "LAST_16", "ROUND_OF_16",
    "QUARTER_FINALS",
    "SEMI_FINALS",
    "FINAL",
    "WINNER",
]


def fetch_matches(api_key):
    """Call football-data.org and return raw matches list (or empty list on error)."""
    if not api_key:
        print("[warn] FOOTBALL_DATA_API_KEY not set; skipping API call (using cached matches.json if present)")
        return None
    url = f"{API_BASE}/competitions/{COMPETITION}/matches"
    req = Request(url, headers={"X-Auth-Token": api_key, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("matches", [])
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"[error] HTTP {e.code} from football-data: {body}")
        return None
    except URLError as e:
        print(f"[error] network: {e}")
        return None


def normalize_matches(raw_matches):
    """Keep only finished matches and slim down to fields we use."""
    keep = []
    for m in raw_matches:
        status = m.get("status")
        if status != "FINISHED":
            continue
        home = (m.get("homeTeam") or {}).get("name")
        away = (m.get("awayTeam") or {}).get("name")
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        # fullTime is score at end of regulation (90' + injury), which is what the bolilla rule wants.
        # NOTE: this excludes goals scored in extra time as required.
        if ft.get("home") is None or ft.get("away") is None:
            continue
        keep.append({
            "id": m.get("id"),
            "utcDate": m.get("utcDate"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
            "home_team": home,
            "away_team": away,
            "home_goals_90": ft["home"],
            "away_goals_90": ft["away"],
            "duration": score.get("duration"),       # REGULAR / EXTRA_TIME / PENALTY_SHOOTOUT
            "winner_overall": score.get("winner"),   # winner including ET/pens (NOT what we use for points)
        })
    return keep


def build_fd_name_to_team(teams):
    """Map football-data name (and a few aliases) -> our team dict."""
    by_name = {}
    aliases = {
        # football-data sometimes uses slightly different names; keep this aligned with teams.json fd_name
        # add aliases as we discover discrepancies in production
    }
    for t in teams:
        if t.get("fd_name"):
            by_name[t["fd_name"].lower()] = t
        # also allow Spanish name lookup
        by_name[t["name"].lower()] = t
    for alias, target in aliases.items():
        if target.lower() in by_name:
            by_name[alias.lower()] = by_name[target.lower()]
    return by_name


def team_match_outcome(team_name, m, fd_lookup):
    """Return ('win'|'draw'|'loss'|None, goals_for, goals_against) for team in match m, using 90' score."""
    home = m["home_team"] or ""
    away = m["away_team"] or ""
    t = fd_lookup.get(team_name.lower())
    if not t:
        return None, 0, 0
    target_fd = (t.get("fd_name") or "").lower()
    if home.lower() in (target_fd, team_name.lower()):
        gf, ga = m["home_goals_90"], m["away_goals_90"]
    elif away.lower() in (target_fd, team_name.lower()):
        gf, ga = m["away_goals_90"], m["home_goals_90"]
    else:
        return None, 0, 0
    if gf > ga:
        return "win", gf, ga
    if gf < ga:
        return "loss", gf, ga
    return "draw", gf, ga


def stage_reached(team_name, matches, fd_lookup):
    """Determine how far a team progressed based on stages of matches they appeared in."""
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
    """Return the name (fd_name) of the World Cup champion, if final is played."""
    finals = [m for m in matches if m.get("stage") == "FINAL"]
    if not finals:
        return None
    final = finals[-1]
    w = final.get("winner_overall")  # for the champion we DO consider overall winner (incl. pens)
    if w == "HOME_TEAM":
        return final["home_team"]
    if w == "AWAY_TEAM":
        return final["away_team"]
    return None


def team_round_bonus(stage_reached_value):
    """Sum of round-progression bonuses earned by reaching that stage.
    Reaching ROUND_OF_32 -> 5; ROUND_OF_16 -> 5 + 7; QF -> +10; SF -> +15; F -> +25; CHAMPION -> +50.
    """
    if not stage_reached_value:
        return 0
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


def compute_team_points(team_name, matches, fd_lookup, is_champion):
    """Compute total points generated by a team and stats."""
    goals_for = 0
    goals_against = 0
    wins = draws = losses = 0
    matches_played = 0
    for m in matches:
        outcome, gf, ga = team_match_outcome(team_name, m, fd_lookup)
        if outcome is None:
            continue
        matches_played += 1
        goals_for += gf
        goals_against += ga
        if outcome == "win":
            wins += 1
        elif outcome == "draw":
            draws += 1
        else:
            losses += 1
    stage = stage_reached(team_name, matches, fd_lookup)
    if is_champion:
        stage = "WINNER"
    rb = team_round_bonus(stage)
    pts_goals = 0.5 * goals_for
    pts_results = 3 * wins + 1 * draws
    total = pts_goals + pts_results + rb
    return {
        "matches_played": matches_played,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "stage_reached": stage or "—",
        "round_bonuses": rb,
        "goals_pts": round(pts_goals, 1),
        "results_pts": pts_results,
        "points_generated": round(total, 1),
    }


def compute_extras_points(participant, extras_official):
    pts = 0
    breakdown = {"pichichi": 0, "mvp": 0, "gk": 0}
    if not extras_official:
        return pts, breakdown
    def matches(a, b):
        return a and b and a.strip().lower() == b.strip().lower()
    if matches(participant.get("pichichi"), extras_official.get("pichichi")):
        pts += 30; breakdown["pichichi"] = 30
    if matches(participant.get("mvp"), extras_official.get("mvp")):
        pts += 30; breakdown["mvp"] = 30
    if matches(participant.get("gk"), extras_official.get("best_gk")):
        pts += 30; breakdown["gk"] = 30
    return pts, breakdown


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

    teams_doc = load_json(TEAMS_FILE, {"teams": [], "group_quotas": {}})
    teams = teams_doc.get("teams", [])
    fd_lookup = build_fd_name_to_team(teams)

    participants_doc = load_json(PARTICIPANTS_FILE, {"participants": [], "tournament_extras": {}})
    participants = participants_doc.get("participants", [])
    extras_official = participants_doc.get("tournament_extras", {}) or {}

    raw = fetch_matches(api_key)
    if raw is None:
        # fallback to cached
        cached = load_json(MATCHES_FILE, {"matches": []})
        normalized = cached.get("matches", [])
        print(f"[info] using {len(normalized)} cached matches")
    else:
        normalized = normalize_matches(raw)
        print(f"[info] fetched {len(raw)} matches, kept {len(normalized)} finished")
        save_json(MATCHES_FILE, {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "matches": normalized,
        })

    champ = champion_team(normalized)

    # Compute team_stats for all teams in catalog
    team_stats = {}
    for t in teams:
        is_champ = bool(champ) and (
            t["name"].lower() == (champ or "").lower() or
            (t.get("fd_name") or "").lower() == (champ or "").lower()
        )
        team_stats[t["name"]] = compute_team_points(t["name"], normalized, fd_lookup, is_champ)

    # Compute ranking
    ranking = []
    for p in participants:
        name = p.get("name", "—")
        selections = p.get("selections", {}) or {}
        team_points_list = []
        sum_goals = sum_results = sum_rounds = 0.0
        for group_key in ["A", "B", "C", "D", "E", "F"]:
            for tname in selections.get(group_key, []) or []:
                s = team_stats.get(tname)
                if not s:
                    team_points_list.append({"name": tname, "group": group_key, "points": 0, "details": {}})
                    continue
                pts = s["points_generated"]
                team_points_list.append({
                    "name": tname,
                    "group": group_key,
                    "points": pts,
                    "details": {
                        "PJ": s["matches_played"], "V": s["wins"], "E": s["draws"], "D": s["losses"],
                        "GF": s["goals_for"], "stage": s["stage_reached"],
                    },
                })
                sum_goals += s["goals_pts"]
                sum_results += s["results_pts"]
                sum_rounds += s["round_bonuses"]
        extras_pts, _ = compute_extras_points(p, extras_official)
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

    save_json(SCOREBOARD_FILE, {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "ranking": ranking,
        "team_stats": team_stats,
        "champion": champ,
    })
    print(f"[ok] wrote {SCOREBOARD_FILE} with {len(ranking)} participants and {len(team_stats)} teams")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Motor de puntuacion de la porra del Mundial 2026.

Lee:
  - data/porra/config.json          (reglas, jugadores, selecciones a pronosticar)
  - data/porra/predictions.json     (pronosticos de cada jugador)
  - data/porra/initial_results.json (resultados oficiales iniciales: ganador, pichichi, etc.)
  - data/matches.json               (partidos finalizados, generado por update_scores.py)
  - data/fixtures.json              (calendario completo)

Escribe:
  - data/porra/porra_scoreboard.json con ranking + breakdown por jugador.

Categorias de puntuacion:
  1) Iniciales (campeon, finalista, tercer puesto, bota oro, balon oro, guante oro)
  2) Selecciones (5 pts por acertar hasta donde llega cada favorita)
  3) Fase de grupos (1 pt por 1X2 acertado en cada partido GROUP_STAGE)
  4) Eliminatorias (1X2 + bonus por resultado exacto en 90', escala por ronda)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PORRA = DATA / "porra"


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Escritura atomica: write a .tmp + rename, asi nginx nunca sirve JSON a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def eq(a, b):
    return bool(a and b and str(a).strip().lower() == str(b).strip().lower())


# ---------- Rank-change annotations ----------

def rankings_equal(a, b):
    if len(a) != len(b):
        return False
    da = {e["name"]: e.get("total", 0) for e in a}
    db = {e["name"]: e.get("total", 0) for e in b}
    return da == db


def annotate_changes(new_ranking, prev_ranking):
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
            e["delta_position"] = ppos - npos
            e["delta_total"] = round(e.get("total", 0) - prev_tot.get(n, 0), 1)
            overtook = []
            if npos < ppos:
                for other in new_ranking:
                    on = other["name"]
                    if on == n:
                        continue
                    op = prev_pos.get(on)
                    if op is None:
                        continue
                    if op < ppos and new_pos[on] > npos:
                        overtook.append(on)
            e["overtook"] = overtook
    return new_ranking


# ---------- Stage ordering and progression ----------
STAGE_TO_LABEL = {
    "GROUP_STAGE":     "Grupos",
    "LAST_32":         "16avos",
    "LAST_16":         "Octavos",
    "QUARTER_FINALS":  "Cuartos",
    "SEMI_FINALS":     "Semifinal",
    "FINAL":           "Final",
    "WINNER":          "Ganador",
}
LABEL_ORDER = ["Grupos", "16avos", "Octavos", "Cuartos", "Semifinal", "Final", "Ganador"]


def team_reached_stage(team_name, matches, champion_name):
    """Calcula la ronda mas avanzada a la que llego un equipo, usando label de stage_options."""
    reached = "Grupos"
    has_match = False
    for m in matches:
        home = (m.get("home_team") or "").strip()
        away = (m.get("away_team") or "").strip()
        if team_name.strip().lower() not in (home.lower(), away.lower()):
            continue
        has_match = True
        st = m.get("stage")
        label = STAGE_TO_LABEL.get(st, "Grupos")
        # If the team played a match in stage X, it means they reached that stage
        if LABEL_ORDER.index(label) > LABEL_ORDER.index(reached):
            reached = label
    if eq(team_name, champion_name):
        reached = "Ganador"
    return reached if has_match else "Grupos"


def champion_team(matches):
    finals = [m for m in matches if m.get("stage") == "FINAL"]
    if not finals:
        return None
    final = finals[-1]
    w = final.get("winner_overall")
    if w == "HOME_TEAM": return final.get("home_team")
    if w == "AWAY_TEAM": return final.get("away_team")
    return None


# ---------- 1) Initial points ----------
def score_initial(prediction, results, scoring, matches):
    """Puntos por pronosticos iniciales.
    Para ganador: depende de la posicion final del equipo predicho (1/2/3).
    Para bota/balon/guante: acierto plano.
    """
    init = prediction.get("initial") or {}
    fs = (results.get("final_standings") or {})
    actual_first  = fs.get("first_place")
    actual_second = fs.get("second_place")
    actual_third  = fs.get("third_place")

    pts = 0
    breakdown = {}

    # The champion pick: if the predicted "first" team actually ends up 1st/2nd/3rd give 15/10/5
    pick_first = init.get("first")
    if pick_first and actual_first:
        if   eq(pick_first, actual_first):  add = scoring["winner_first_place"];  why = "Acertaste 1º"
        elif eq(pick_first, actual_second): add = scoring["winner_second_place"]; why = "Tu 1º acabo 2º"
        elif eq(pick_first, actual_third):  add = scoring["winner_third_place"];  why = "Tu 1º acabo 3º"
        else: add = 0; why = ""
        if add: pts += add; breakdown["winner_pick"] = {"pick": pick_first, "points": add, "note": why}

    # Top scorer / best player / best keeper: exact match
    if eq(init.get("pichichi"), results.get("top_scorer")):
        pts += scoring["top_scorer"]; breakdown["top_scorer"] = scoring["top_scorer"]
    if eq(init.get("mvp"), results.get("best_player")):
        pts += scoring["best_player"]; breakdown["best_player"] = scoring["best_player"]
    if eq(init.get("gk"), results.get("best_keeper")):
        pts += scoring["best_keeper"]; breakdown["best_keeper"] = scoring["best_keeper"]

    return round(pts, 1), breakdown


# ---------- 2) Selections progression ----------
def score_selections(prediction, scoring, predicted_selections, matches, champion):
    selections = prediction.get("selections") or {}
    per = scoring["per_correct"]
    pts = 0
    details = {}
    for team in predicted_selections:
        predicted = selections.get(team)
        if not predicted:
            continue
        actual_label = team_reached_stage(team, matches, champion)
        # Only score if the team has finished its run (we score "exact stage reached")
        # For now we score progressively: if predicted matches reached label exactly.
        # Note: a team is "done" only after eliminated. While still playing, we don't
        # know the final ronda. We score whatever its current furthest stage is, but
        # if the actual exceeds predicted, the prediction is already wrong.
        if predicted == actual_label:
            pts += per; details[team] = {"predicted": predicted, "actual": actual_label, "points": per}
        else:
            details[team] = {"predicted": predicted, "actual": actual_label, "points": 0}
    return round(pts, 1), details


# ---------- 3) Group stage 1X2 ----------
def result_1x2(home_goals, away_goals):
    if home_goals > away_goals: return "1"
    if home_goals < away_goals: return "2"
    return "X"


def score_group_stage(prediction, scoring, matches):
    gs = prediction.get("group_stage") or {}
    pts = 0
    correct = 0
    total = 0
    pt = scoring["result_1x2"]
    for m in matches:
        if m.get("stage") != "GROUP_STAGE":
            continue
        mid = str(m.get("id"))
        pick = gs.get(mid) or gs.get(int(mid)) or None
        if not pick:
            continue
        total += 1
        actual = result_1x2(m.get("home_goals_90", 0), m.get("away_goals_90", 0))
        if pick == actual:
            pts += pt; correct += 1
    return round(pts, 1), {"correct": correct, "answered": total}


# ---------- 4) Knockouts ----------
def score_knockouts(prediction, scoring, matches):
    ko = prediction.get("knockouts") or {}
    pts = 0
    details = {"exact": 0, "result_only": 0, "wrong": 0}
    for m in matches:
        st = m.get("stage")
        if st not in scoring:
            continue
        mid = str(m.get("id"))
        pick = ko.get(mid) or ko.get(int(mid))
        if not pick:
            continue
        try:
            ph = int(pick.get("home"))
            pa = int(pick.get("away"))
        except (TypeError, ValueError):
            continue
        ah = m.get("home_goals_90", 0)
        aa = m.get("away_goals_90", 0)
        pick_res = result_1x2(ph, pa)
        actual_res = result_1x2(ah, aa)
        rules = scoring[st]
        if ph == ah and pa == aa:
            pts += rules["result_1x2"] + rules["exact_bonus"]
            details["exact"] += 1
        elif pick_res == actual_res:
            pts += rules["result_1x2"]
            details["result_only"] += 1
        else:
            details["wrong"] += 1
    return round(pts, 1), details


def main():
    config = load_json(PORRA / "config.json", {"players": [], "predicted_selections": [], "scoring": {}})
    predictions_doc = load_json(PORRA / "predictions.json", {"predictions": {}})
    results = load_json(PORRA / "initial_results.json", {})
    matches_doc = load_json(DATA / "matches.json", {"matches": []})
    matches = matches_doc.get("matches", [])

    scoring = config.get("scoring") or {}
    champion = champion_team(matches)

    ranking = []
    for player_name in config.get("players", []):
        pred = predictions_doc.get("predictions", {}).get(player_name) or {}
        pts_init, bd_init = score_initial(pred, results, scoring.get("initial", {}), matches)
        pts_sel,  bd_sel  = score_selections(pred, scoring.get("selections_progression", {}), config.get("predicted_selections", []), matches, champion)
        pts_gs,   bd_gs   = score_group_stage(pred, scoring.get("group_stage", {}), matches)
        pts_ko,   bd_ko   = score_knockouts(pred, scoring.get("knockouts", {}), matches)
        total = round(pts_init + pts_sel + pts_gs + pts_ko, 1)
        ranking.append({
            "name": player_name,
            "total": total,
            "breakdown": {
                "initial":     pts_init,
                "selections":  pts_sel,
                "group_stage": pts_gs,
                "knockouts":   pts_ko,
            },
            "details": {
                "initial":     bd_init,
                "selections":  bd_sel,
                "group_stage": bd_gs,
                "knockouts":   bd_ko,
            },
            "has_submitted": bool(pred and pred.get("submitted_at")),
        })

    ranking.sort(key=lambda x: (-x["total"], x["name"].lower()))

    # Sticky previous + anotacion de movimientos
    prev_doc = load_json(PORRA / "porra_scoreboard.json", {})
    last_persisted_ranking = prev_doc.get("ranking", [])
    sticky_previous = prev_doc.get("previous_ranking", [])
    if rankings_equal(last_persisted_ranking, ranking):
        chosen_prev = sticky_previous
    else:
        chosen_prev = last_persisted_ranking
    annotate_changes(ranking, chosen_prev)

    save_json(PORRA / "porra_scoreboard.json", {
        "_comment": "Generado por scripts/update_porra.py. No editar a mano.",
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "champion": champion,
        "ranking": ranking,
        "previous_ranking": chosen_prev,
    })
    print(f"[ok] porra: {len(ranking)} jugadores procesados (campeon={champion})")


if __name__ == "__main__":
    main()

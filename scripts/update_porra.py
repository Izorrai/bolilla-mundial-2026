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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PORRA = DATA / "porra"


def load_json(path, default):
    """Lee JSON tolerando archivos vacios o corruptos."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return default
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] {path} corrupto/vacio ({e}); usando default")
        return default


def save_json(path, data):
    """Escritura atomica: write a .tmp + rename, asi nginx nunca sirve JSON a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def _norm_name(s):
    """Normaliza para comparar: strippea acentos, baja a minusculas, colapsa espacios.
    'Unai Simón' -> 'unai simon'."""
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", str(s))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.strip().lower().split())


def eq(a, b):
    """Comparacion laxa de nombres para acertar premios (pichichi, mvp, portero).
    Ignora acentos y acepta que el pick sea un subconjunto de palabras del real
    (o viceversa), asi 'Mbappe' cuenta como acierto de 'Kylian Mbappé' y
    'Unai Simon' cuenta como acierto de 'Unai Simón'. No confunde nombres
    distintos: 'Kane' vs 'Rodrigo Hernández' sigue devolviendo False."""
    na = _norm_name(a)
    nb = _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta = set(na.split())
    tb = set(nb.split())
    return ta.issubset(tb) or tb.issubset(ta)


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


def _team_in_match(team_name, m):
    home = (m.get("home_team") or "").strip().lower()
    away = (m.get("away_team") or "").strip().lower()
    return team_name.strip().lower() in (home, away)


def team_reached_stage(team_name, matches, champion_name):
    """Calcula la ronda mas avanzada a la que llego un equipo, usando label de stage_options."""
    reached = "Grupos"
    has_match = False
    for m in matches:
        if not _team_in_match(team_name, m):
            continue
        has_match = True
        st = m.get("stage")
        label = STAGE_TO_LABEL.get(st, "Grupos")
        if LABEL_ORDER.index(label) > LABEL_ORDER.index(reached):
            reached = label
    if eq(team_name, champion_name):
        reached = "Ganador"
    return reached if has_match else "Grupos"


def team_is_eliminated(team_name, matches, fixtures):
    """Devuelve True si el equipo ya ha sido eliminado del torneo."""
    tn = team_name.strip().lower()

    # Si tiene algun partido pendiente (TIMED/SCHEDULED/IN_PLAY/PAUSED), sigue vivo.
    # El partido por el 3er/4o puesto (THIRD_PLACE) se ignora aqui: perder la
    # semifinal ya fija el resultado a efectos de "hasta donde llego", el
    # partido de consolacion no hace avanzar de ronda y no debe mantener al
    # equipo colgado en "En juego" hasta que se dispute.
    for f in fixtures:
        if not _team_in_match(team_name, f):
            continue
        if f.get("stage") == "THIRD_PLACE":
            continue
        if f.get("status") not in ("FINISHED",):
            return False

    # Si no tiene ningun partido en absoluto, no podemos saber -> no eliminado aun
    team_matches = [m for m in matches if _team_in_match(team_name, m)]
    if not team_matches:
        return False

    # Tiene partidos y todos estan FINISHED (ningun pendiente, salvo 3er puesto) -> eliminado
    return True


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
def score_selections(prediction, scoring, predicted_selections, matches, champion, fixtures, name_to_fd=None):
    selections = prediction.get("selections") or {}
    name_to_fd = name_to_fd or {}
    per = scoring["per_correct"]
    pts = 0
    details = {}
    for team in predicted_selections:
        predicted = selections.get(team)
        if not predicted:
            continue
        # Las selecciones van en español (Alemania) pero los partidos vienen de
        # la API en inglés (Germany). Traducimos antes de buscar los partidos,
        # si no ningún equipo casa y todos salen "En juego" (incluidos eliminados).
        team_fd = name_to_fd.get(team.strip().lower(), team)
        actual_label = team_reached_stage(team_fd, matches, champion)
        eliminated = team_is_eliminated(team_fd, matches, fixtures)
        if not eliminated:
            details[team] = {"predicted": predicted, "actual": "En juego", "points": 0}
            continue
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
    match_list = []
    for m in matches:
        if m.get("stage") != "GROUP_STAGE":
            continue
        mid = str(m.get("id"))
        pick = gs.get(mid) or gs.get(int(mid)) or None
        if not pick:
            continue
        total += 1
        ah = m.get("home_goals_90", 0)
        aa = m.get("away_goals_90", 0)
        actual = result_1x2(ah, aa)
        ok = pick == actual
        if ok:
            pts += pt; correct += 1
        match_list.append({
            "id": mid,
            "home_team": m.get("home_team"),
            "away_team": m.get("away_team"),
            "actual_home": ah, "actual_away": aa,
            "pick": pick, "actual": actual,
            "ok": ok, "points": pt if ok else 0,
        })
    return round(pts, 1), {"correct": correct, "answered": total, "matches": match_list}


# ---------- 4) Knockouts ----------
def score_knockouts(prediction, scoring, matches):
    ko = prediction.get("knockouts") or {}
    pts = 0
    details = {"exact": 0, "result_only": 0, "wrong": 0, "matches": []}
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
        match_pts = 0
        if ph == ah and pa == aa:
            match_pts = rules["result_1x2"] + rules["exact_bonus"]
            details["exact"] += 1
            verdict = "exact"
        elif pick_res == actual_res:
            match_pts = rules["result_1x2"]
            details["result_only"] += 1
            verdict = "result_only"
        else:
            details["wrong"] += 1
            verdict = "wrong"
        pts += match_pts
        details["matches"].append({
            "id": mid,
            "home_team": m.get("home_team"),
            "away_team": m.get("away_team"),
            "pick_home": ph, "pick_away": pa,
            "actual_home": ah, "actual_away": aa,
            "points": match_pts,
            "verdict": verdict,
            "stage": st,
        })
    return round(pts, 1), details


def main():
    config = load_json(PORRA / "config.json", {"players": [], "predicted_selections": [], "scoring": {}})
    predictions_doc = load_json(PORRA / "predictions.json", {"predictions": {}})
    results = load_json(PORRA / "initial_results.json", {})
    matches_doc = load_json(DATA / "matches.json", {"matches": []})
    matches = matches_doc.get("matches", [])
    fixtures_doc = load_json(DATA / "fixtures.json", {"fixtures": []})
    fixtures = fixtures_doc.get("fixtures", [])
    teams_doc = load_json(DATA / "teams.json", {"teams": []})
    # Mapa nombre en español (name) -> nombre de la API (fd_name), para casar las
    # selecciones (español) con los equipos de los partidos (inglés).
    name_to_fd = {}
    for t in teams_doc.get("teams", []):
        es = (t.get("name") or "").strip().lower()
        fd = t.get("fd_name")
        if es and fd:
            name_to_fd[es] = fd

    scoring = config.get("scoring") or {}
    champion = champion_team(matches)

    ranking = []
    for player_name in config.get("players", []):
        pred = predictions_doc.get("predictions", {}).get(player_name) or {}
        pts_init, bd_init = score_initial(pred, results, scoring.get("initial", {}), matches)
        pts_sel,  bd_sel  = score_selections(pred, scoring.get("selections_progression", {}), config.get("predicted_selections", []), matches, champion, fixtures, name_to_fd)
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

    # Previous_ranking solo se actualiza cuando termina un partido nuevo.
    # Si el previo es stale (distintos jugadores), reset duro a foto actual.
    prev_doc = load_json(PORRA / "porra_scoreboard.json", {})
    last_persisted_ranking = prev_doc.get("ranking", [])
    sticky_previous = prev_doc.get("previous_ranking", [])
    prev_finished_count = prev_doc.get("finished_matches_count", len(matches))
    prev_compatible = bool(sticky_previous) and (
        {e.get("name") for e in sticky_previous} == {e.get("name") for e in ranking}
    )
    if not prev_compatible:
        chosen_prev = ranking
    elif len(matches) > prev_finished_count:
        chosen_prev = last_persisted_ranking
    else:
        chosen_prev = sticky_previous
    annotate_changes(ranking, chosen_prev)

    save_json(PORRA / "porra_scoreboard.json", {
        "_comment": "Generado por scripts/update_porra.py. No editar a mano.",
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "champion": champion,
        "ranking": ranking,
        "previous_ranking": chosen_prev,
        "finished_matches_count": len(matches),
    })
    print(f"[ok] porra: {len(ranking)} jugadores procesados (campeon={champion})")


if __name__ == "__main__":
    main()

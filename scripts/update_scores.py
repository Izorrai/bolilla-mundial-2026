#!/usr/bin/env python3
"""
Motor de puntuacion de la Bolilla Mundial 2026 (multi-sala).

Lee:
  - data/teams.json                              (catalogo compartido de 48 selecciones)
  - data/rooms.json                              (listado de salas)
  - data/rooms/<room_id>/participants.json       (inscripciones de cada sala)
  - data/fixtures.json                           (calendario completo, incluidos partidos
                                                    programados/en directo, generado por
                                                    fetch_fixtures.py)

Llama a football-data.org /v4/competitions/WC/matches (una sola vez por ejecucion) y:
  - Guarda los partidos finalizados en data/matches.json
  - Para cada sala, calcula puntos por participante y escribe
    data/rooms/<room_id>/scoreboard.json

Reglas (recordatorio):
  Solo cuentan los 90 minutos (score.fullTime). Sin prorroga ni penaltis (excepto campeon).
  gol 0.5  victoria 3  empate 1  derrota 0
  bonus acumulado por ronda alcanzada: 16avos 5 / octavos 7 / cuartos 10 / semis 15 / final 25 / campeon 50

  IMPORTANTE: el bonus de ronda se concede en cuanto se CONFIRMA el cruce
  (el equipo tiene un partido asignado en esa fase en fixtures.json, aunque
  ese partido concreto todavia no se haya jugado), no cuando termina el
  partido de esa fase. Se usa scheduled_stage_reached() para esto, combinado
  con stage_reached() (que mira solo partidos FINISHED) por si fixtures.json
  no estuviera disponible en algun momento.

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
FIXTURES_FILE = DATA / "fixtures.json"
STAGE_LOCK_FILE = DATA / "stage_lock.json"

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
    """Fase mas avanzada en la que el equipo tiene un partido FINALIZADO.
    Se mantiene tal cual para los stats de PJ/V/E/D y goles, que solo deben
    contar partidos ya jugados."""
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


def scheduled_stage_reached(team_name, all_fixtures, fd_lookup):
    """A diferencia de stage_reached() (que solo mira partidos FINISHED),
    esta funcion mira TODOS los fixtures (incluidos programados/en directo,
    es decir data/fixtures.json completo) para detectar la fase mas avanzada
    en la que el equipo tiene un partido ASIGNADO, aunque ese partido en
    concreto aun no se haya jugado.

    Esto permite que el bonus de ronda se conceda en cuanto se confirma el
    cruce (p.ej. el calendario de dieciseisavos sale publicado), en vez de
    esperar a que termine ese partido."""
    reached = "GROUP_STAGE"
    has_any = False
    t = fd_lookup.get(team_name.lower())
    if not t:
        return None
    target_fd = (t.get("fd_name") or "").lower()
    target_name = team_name.lower()
    for f in all_fixtures:
        home = (f.get("home_team") or "").lower()
        away = (f.get("away_team") or "").lower()
        if home not in (target_fd, target_name) and away not in (target_fd, target_name):
            continue
        has_any = True
        st = f.get("stage")
        if st in STAGE_ORDER and STAGE_ORDER.index(st) > STAGE_ORDER.index(reached):
            reached = st
    return reached if has_any else None


def apply_stage_lock(team_name, candidate_stage, stage_lock):
    """Candado MONOTONICO sobre la fase confirmada de cada equipo, igual que
    ya se hace con matches.json para los partidos finalizados.

    football-data.org puede "titubear" mientras regenera el cuadro de
    eliminatorias (a veces devuelve fixtures.json con el cruce ya asignado,
    a veces lo vuelve a devolver como "? vs ?" / SCHEDULED sin equipos).
    Sin este candado, el bonus de ronda aparece y desaparece del ranking
    entre ejecuciones del cron, lo cual es muy confuso para la gente.

    stage_lock es un dict {team_name: stage} persistido en disco
    (data/stage_lock.json). Esta funcion compara la fase candidata de esta
    ejecucion con la maxima ya registrada y SOLO deja que suba, nunca que
    baje. Devuelve la fase final a usar y muta stage_lock in-place."""
    if not candidate_stage:
        # Sin dato esta vez: nos quedamos con lo que ya teniamos bloqueado
        # (o GROUP_STAGE si es la primera vez que vemos a este equipo).
        return stage_lock.get(team_name, "GROUP_STAGE")
    locked = stage_lock.get(team_name, "GROUP_STAGE")
    if candidate_stage in STAGE_ORDER and locked in STAGE_ORDER:
        if STAGE_ORDER.index(candidate_stage) > STAGE_ORDER.index(locked):
            stage_lock[team_name] = candidate_stage
            return candidate_stage
        return locked
    # Valor de fase desconocido (no deberia pasar): no tocamos el candado.
    return locked


def stages_fully_confirmed(all_fixtures):
    """Devuelve el set de fases (de STAGE_ORDER) cuyo cuadro esta COMPLETO:
    todos los partidos de esa fase en fixtures.json tienen ya home_team y
    away_team asignados (ningun "? vs ?" / null pendiente de sorteo o de que
    termine la fase anterior).

    Esto se usa para decidir si el bonus de ronda de una fase eliminatoria
    se reparte ya o se espera. La idea: en vez de ir dando el bonus equipo
    a equipo a medida que la API confirma cruces sueltos (lo cual genera
    parpadeos cuando la API titubea a medio confirmar), esperamos a que
    los 16/8/4/2 cruces de la fase estén completos y entonces se conceden
    todos los bonus de esa fase de golpe, a la vez, para todo el mundo.

    GROUP_STAGE no se incluye aqui: la fase de grupos siempre se considera
    "confirmada" desde el principio (no tiene cruces por definir)."""
    by_stage = {}
    for f in all_fixtures:
        st = f.get("stage")
        if st not in STAGE_ORDER or st in ("GROUP_STAGE", "WINNER"):
            continue
        by_stage.setdefault(st, []).append(f)

    confirmed = set()
    for st, fixtures_in_stage in by_stage.items():
        if not fixtures_in_stage:
            continue
        complete = all(
            (f.get("home_team") or "").strip() and (f.get("away_team") or "").strip()
            for f in fixtures_in_stage
        )
        if complete:
            confirmed.add(st)
    return confirmed


def highest_fully_confirmed_stage(stage_value, confirmed_stages):
    """Recorta una fase candidata hacia abajo hasta la fase mas alta que
    este totalmente confirmada. P.ej. si un equipo tiene partido asignado
    en QUARTER_FINALS pero esa fase aun no esta completa para todos los
    cruces (solo se conocen algunos), bajamos su fase a la ultima que si
    este completa (p.ej. LAST_16), para no soltar el bonus de cuartos antes
    de tiempo. GROUP_STAGE siempre se considera valida (no requiere
    confirmacion de cruces)."""
    if not stage_value or stage_value not in STAGE_ORDER:
        return stage_value
    idx = STAGE_ORDER.index(stage_value)
    while idx > 0:
        candidate = STAGE_ORDER[idx]
        if candidate in ("GROUP_STAGE", "WINNER") or candidate in confirmed_stages:
            return candidate
        idx -= 1
    return STAGE_ORDER[0]


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


def compute_team_stats(team_name, matches, fd_lookup, is_champion, all_fixtures=None, stage_lock=None, confirmed_stages=None):
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

    # Fase para mostrar en stats (PJ/V/E/D ya jugados): la "jugada" tal cual.
    played_stage = stage_reached(team_name, matches, fd_lookup)

    # Fase candidata para el BONUS de ronda: la mas avanzada entre "jugada"
    # y "confirmada por calendario" (fixtures.json incluye partidos futuros).
    bonus_stage = played_stage
    if all_fixtures:
        sched_stage = scheduled_stage_reached(team_name, all_fixtures, fd_lookup)
        if sched_stage and (
            not bonus_stage
            or STAGE_ORDER.index(sched_stage) > STAGE_ORDER.index(bonus_stage)
        ):
            bonus_stage = sched_stage

    # TODO O NADA POR FASE: recortamos la fase candidata a la ultima fase
    # eliminatoria que este COMPLETAMENTE confirmada (los 16/8/4/2 cruces
    # con ambos equipos ya asignados). Asi el bonus de una fase se reparte
    # a todo el mundo de golpe, en vez de ir goteando equipo a equipo
    # mientras la API aun esta confirmando cruces sueltos.
    if confirmed_stages is not None:
        bonus_stage = highest_fully_confirmed_stage(bonus_stage, confirmed_stages)

    # CANDADO: si la API titubea y esta vez "bonus_stage" sale mas bajo que
    # lo que ya teniamos confirmado en una ejecucion anterior, nos quedamos
    # con el valor bloqueado. Asi el bonus de ronda nunca desaparece del
    # ranking una vez concedido.
    if stage_lock is not None and not is_champion:
        bonus_stage = apply_stage_lock(team_name, bonus_stage, stage_lock)

    if is_champion:
        bonus_stage = "WINNER"
        if stage_lock is not None:
            stage_lock[team_name] = "WINNER"

    rb = team_round_bonus(bonus_stage)
    pts_goals = 0.5 * goals_for
    pts_results = 3 * wins + 1 * draws
    return {
        "matches_played": matches_played,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "wins": wins, "draws": draws, "losses": losses,
        "stage_reached": bonus_stage or played_stage or "—",
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
            if npos < ppos:
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
    """Lee JSON tolerando archivos vacios o corruptos: devuelve el default
    en vez de petar. Asi el siguiente run de los scripts puede regenerar
    el archivo en lugar de quedarse atascado para siempre."""
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
    """Escritura ATOMICA: escribe a .tmp y hace rename. Asi nginx nunca
    sirve un JSON a medias durante la escritura."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)  # os.rename atomico en el mismo filesystem


def main():
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()

    teams_doc = load_json(TEAMS_FILE, {"teams": []})
    teams = teams_doc.get("teams", [])
    fd_lookup = build_fd_name_to_team(teams)

    rooms_doc = load_json(ROOMS_FILE, {"rooms": [{"id": "default", "name": "Default"}]})
    rooms = rooms_doc.get("rooms", [])

    # Calendario completo (incluye partidos programados/en directo), generado
    # por fetch_fixtures.py. Se usa SOLO para detectar la fase "confirmada"
    # de cada equipo (scheduled_stage_reached), no para calcular goles/resultados.
    fixtures_doc = load_json(FIXTURES_FILE, {"fixtures": []})
    all_fixtures = fixtures_doc.get("fixtures", [])

    # Candado monotonico de fases confirmadas por equipo (ver apply_stage_lock).
    # Persiste entre ejecuciones del cron para que el bonus de ronda nunca
    # desaparezca si la API titubea con el cuadro de eliminatorias.
    stage_lock = load_json(STAGE_LOCK_FILE, {})

    # Fases eliminatorias cuyo cuadro esta completo (todos los cruces ya
    # tienen ambos equipos asignados). Se usa para repartir el bonus de
    # ronda de golpe a todos los equipos de esa fase, no equipo a equipo.
    confirmed_stages = stages_fully_confirmed(all_fixtures)
    if confirmed_stages:
        print(f"[info] fases eliminatorias completamente confirmadas: {sorted(confirmed_stages, key=STAGE_ORDER.index)}")

    raw = fetch_matches(api_key)
    cached_doc = load_json(MATCHES_FILE, {"matches": []})
    cached_matches = cached_doc.get("matches", [])
    cached_by_id = {m.get("id"): m for m in cached_matches if m.get("id") is not None}

    if raw is None:
        matches = cached_matches
        print(f"[info] using {len(matches)} cached matches (API caida)")
    else:
        new_matches = normalize_matches(raw)
        new_by_id = {m.get("id"): m for m in new_matches if m.get("id") is not None}

        # MONOTONICO: nunca perdemos un partido que ya teniamos como FINISHED
        # con marcador valido. Si la API devuelve menos partidos finalizados
        # (estados transitorios FINISHED/null durante VAR, hipos de la API,
        # match que pasa brevemente a IN_PLAY...), conservamos el dato anterior.
        # Solo aceptamos cambios cuando el nuevo dato es valido y "mejora o iguala"
        # al anterior.
        merged_by_id = dict(cached_by_id)  # arranca con todo lo que teniamos
        for mid, m in new_by_id.items():
            merged_by_id[mid] = m  # update / insert: el nuevo dato gana si existe

        # Detectamos si se perdieron partidos
        lost = set(cached_by_id) - set(new_by_id)
        if lost:
            print(f"[warn] API devolvio {len(lost)} partidos menos que el cache; los mantenemos para no perder puntos")

        matches = list(merged_by_id.values())
        # Orden estable por fecha
        matches.sort(key=lambda m: (m.get("utcDate") or "", m.get("id") or 0))

        print(f"[info] fetched {len(raw)} matches, {len(new_matches)} new FINISHED valid, total acumulado {len(matches)}")
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
        team_stats[t["name"]] = compute_team_stats(
            t["name"], matches, fd_lookup, is_champ, all_fixtures, stage_lock, confirmed_stages
        )

    # Persistimos el candado actualizado para la siguiente ejecucion.
    save_json(STAGE_LOCK_FILE, stage_lock)

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
        # Cuantos partidos FINISHED habia la ultima vez que actualizamos el
        # scoreboard. Si no esta (primera ejecucion con esta logica),
        # inicializamos al numero actual para no generar deltas "fantasma".
        prev_finished_count = prev_scoreboard.get("finished_matches_count", len(matches))

        ranking = compute_room_scoreboard(room_id, participants_doc, team_stats, teams)

        # Decision del "previous_ranking" usado para calcular deltas:
        # 1. Si el previo guardado esta stale (distintos participantes que el
        #    ranking actual, p.ej. tras un deploy o tras anadir gente) → RESET
        #    a la foto actual. Sin deltas este run, las flechas se quedan en
        #    blanco hasta que termine un partido nuevo.
        # 2. Si ha terminado un partido nuevo (len matches subio) → la ultima
        #    foto persistida pasa a ser el previo, generando deltas reales.
        # 3. Si no hay novedad → mantenemos el previo de la ultima rotacion,
        #    asi los deltas no se mueven entre crons.
        prev_compatible = bool(sticky_previous) and (
            {e.get("name") for e in sticky_previous} == {e.get("name") for e in ranking}
        )
        if not prev_compatible:
            chosen_prev = ranking  # reset duro → todo a 0
        elif len(matches) > prev_finished_count:
            chosen_prev = last_persisted_ranking
        else:
            chosen_prev = sticky_previous

        annotate_changes(ranking, chosen_prev)

        save_json(scoreboard_file, {
            "last_updated": now_iso,
            "ranking": ranking,
            "previous_ranking": chosen_prev,
            "finished_matches_count": len(matches),
            "team_stats": team_stats,
            "champion": champ,
            "room_id": room_id,
            "room_name": room.get("name", room_id),
        })
        print(f"[ok] room '{room_id}': {len(ranking)} participants -> {scoreboard_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

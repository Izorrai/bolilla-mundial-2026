#!/usr/bin/env python3
"""
Genera el feed de "Cronica" — frases automaticas que comentan los movimientos
en la clasificacion (adelantamientos, subidas/caidas grandes, nuevo lider, etc.).

Lee scoreboards generados por update_scores.py y update_porra.py, detecta los
eventos comparando con el ultimo estado "visto" guardado en el feed, y genera
una frase aleatoria con uno de los 5 personajes (Cuñao, Cronista, Sarcastico,
Meme, El de Residuos). Por defecto solo se genera el feed por sala, sin que
ninguna pagina publica lo muestre todavia (la preview-feed.html lo expone).

Salida:
  data/feed/<room_id>.json (bolilla)
  data/feed/porra.json     (porra)
"""

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FEED_DIR = DATA / "feed"
ROOMS_FILE = DATA / "rooms.json"
PORRA_SCOREBOARD = DATA / "porra" / "porra_scoreboard.json"

# Cuantos eventos guardamos en cada feed
MAX_EVENTS = 80
# Cuantas frases recientes no se repiten
ANTI_REPEAT_WINDOW = 20


# ---------- Personajes y reparto por sala ----------
# Pesos por personaje, por sala. La oficina (GrupoAgaleus) se lleva mas
# residuos. La peña de Deusto, mas cuñao/cronista. Porra es mezcla neutral.
PERSONAJE_WEIGHTS = {
    "grupoagaleus":          {"residuos": 40, "sarcastico": 20, "cronista": 15, "cuñao": 15, "meme": 10},
    "deustobarakaultimate":  {"cuñao": 30, "cronista": 25, "sarcastico": 25, "meme": 20, "residuos": 0},
    "_porra":                {"cronista": 30, "cuñao": 25, "sarcastico": 25, "meme": 20, "residuos": 0},
    "_default":              {"cronista": 25, "cuñao": 25, "sarcastico": 25, "meme": 15, "residuos": 10},
}

CLOSERS = ["🥊", "📉", "👀", "🍿", "🔥", "💥", "🚀", "💀", "🗑️", "📦", "⚖️", "📊", "🚛", "", "", ""]


# ---------- Plantillas ----------
# Slots: {actor}, {victim}, {delta}, {prev_leader}, {prev_last}
# Cuanto mayor el banco, menos repeticion. Empiezo con ~6-8 por personaje/evento.

T = {
    # ============= OVERTAKE (adelantamiento) =============
    ("overtake", "cuñao"): [
        "Hala, {actor} se acaba de merendar a {victim}. ¡Lo que les digo yo!",
        "¡Pero qué hace {actor} pasándole por encima a {victim}!",
        "{actor} le ha calzado un buen adelantamiento a {victim}, casi se da con la valla.",
        "Mira tú, {actor} dejando a {victim} con cara de tonto.",
        "Esto es lo que les digo yo a mis cuñaos: {actor} le acaba de comer la merienda a {victim}.",
        "{victim} se ha despistado y {actor} se ha colado por la rendija.",
    ],
    ("overtake", "cronista"): [
        "¡Atención! {actor} se planta delante de {victim} en la recta final.",
        "Y por la banda izquierda aparece {actor}, fulminando a {victim}.",
        "¡{actor} no perdona! {victim}, golpeado y hundido en la tabla.",
        "Adelantamiento de manual: {actor} deja atrás a {victim}.",
        "Ohhh, qué hace {actor}. Adelanta a {victim} como si nada.",
        "Cambio de posiciones: {actor} arriba, {victim} a sufrir.",
    ],
    ("overtake", "sarcastico"): [
        "Vaya, mira quién se ha decidido a aparecer: {actor} pasa a {victim}.",
        "Sorprendente, sorprendente. {actor} delante de {victim}. Quién lo iba a decir.",
        "{victim}, parece que {actor} tenía algo que decirte. Ya está dicho.",
        "Cosas que pasan: {actor} acaba de adelantar a {victim}. Qué original.",
        "Pues sí. {actor} ha decidido respirar y ha pasado a {victim} sin esforzarse.",
        "{actor} pasa a {victim}. Lo raro habría sido que no.",
    ],
    ("overtake", "meme"): [
        "{actor} +1 puesto. {victim} -1 en autoestima.",
        "POV: {victim} viendo a {actor} adelantarle. Carita triste.",
        "{actor} habiendo sido invitada al podio antes que {victim}. Cringe.",
        "{victim}: 'no puede ser'. {actor}: 'pues sí'.",
        "{actor} acaba de adelantar a {victim}. Speedrun any%.",
        "{victim} mainstream, {actor} indie. {actor} arriba.",
    ],
    ("overtake", "residuos"): [
        "{actor} tira a {victim} al contenedor del rechazo.",
        "Trasvase entre cubetas: {actor} delante de {victim}.",
        "{actor} compacta a {victim} como palet de cartón.",
        "{victim} queda lixiviando en el fondo, {actor} ya está en el azul.",
        "{actor} aplica inertización a {victim}. Material neutralizado.",
        "{actor} le ha pasado por encima como camión cisterna a {victim}.",
        "Báscula descalibrada para {victim}: ajuste a la baja, {actor} sube.",
        "DCS firmado y sellado de {actor}, {victim} se queda sin tramitar.",
        "{actor} factura el adelantamiento. {victim}, vencimiento ya.",
        "Contrato de tratamiento ampliado para {actor}, {victim} pendiente de renovación.",
    ],

    # ============= BIG_UP (subida >=3) =============
    ("big_up", "cuñao"): [
        "¡Ojo cuidado! {actor} sube {delta} puestos del tirón. ¡Esto es lo que les digo!",
        "Hala con {actor}, pega un salto de {delta} posiciones. ¿Esto cómo se hace?",
        "Pero {actor}, ¿de dónde sales con esas? Te has plantado {delta} puestos arriba.",
        "{actor} sube {delta} puestos. Si esto fuera el bar pagaba la siguiente ronda.",
        "Madre del amor hermoso, {delta} puestos para {actor} en un parpadeo.",
    ],
    ("big_up", "cronista"): [
        "¡Y se desata {actor}! Sube {delta} puestos de golpe.",
        "¡Atención al sprint de {actor}: +{delta} en la tabla!",
        "Esto es una locura, {actor} escala {delta} posiciones como si nada.",
        "¡Y {actor} entra como un toro! +{delta} puestos.",
        "Pumba, {actor} se planta {delta} puestos arriba.",
    ],
    ("big_up", "sarcastico"): [
        "Mira por dónde, {actor} despierta y sube {delta} puestos. Tarde piaste.",
        "Sí, claro, {actor} sube {delta} posiciones. Todo normal.",
        "{actor} +{delta}. ¿Trampa? No nos atrevemos a decirlo.",
        "Vaya, parece que {actor} se ha tomado un café. {delta} puestos arriba.",
        "Pues {actor} ahí está, escalando {delta} puestos. Qué casualidad.",
    ],
    ("big_up", "meme"): [
        "{actor} +{delta} puestos. ¿Información privilegiada o suerte cósmica?",
        "{actor} subiendo {delta} puestos: speedrun activado.",
        "POV: eres {actor} y has subido {delta} puestos del tirón. GG.",
        "{actor} desbloqueando logro: +{delta} pos. Like si lo sabías.",
        "{actor} +{delta}. El resto de la sala: 👀",
    ],
    ("big_up", "residuos"): [
        "Pase de báscula exitoso: {actor} +{delta} toneladas de puntos.",
        "{actor} sube {delta} puestos. Recogida selectiva impecable.",
        "Compactador en marcha: {actor} +{delta} en bala lista para retirar.",
        "Aceite usado bien valorizado: {actor} sube {delta}.",
        "{actor} gana licitación de la jornada: +{delta} puestos.",
        "Auditoría superada con nota: {actor} +{delta}.",
        "Inertización exitosa: {actor} sube {delta} sin manchar.",
    ],

    # ============= BIG_DOWN (caida >=3) =============
    ("big_down", "cuñao"): [
        "¡Anda que {actor}! Pierde {delta} puestos. Habrá que llamar al médico.",
        "{actor} se pega un trompazo de {delta} posiciones. ¡Madre mía!",
        "{actor} -{delta}. ¿Pero qué le pasa al chaval?",
        "Esto no se hace, {actor}. -{delta} puestos del tirón.",
        "Hala, {actor} para abajo {delta} puestos. Tira pa'l bar.",
    ],
    ("big_down", "cronista"): [
        "¡Caída brutal de {actor}! -{delta} en la clasificación.",
        "¡Hundimiento! {actor} pierde {delta} posiciones de golpe.",
        "Y {actor} se viene abajo: {delta} puestos menos.",
        "Catástrofe para {actor}: cae {delta} puestos en una jornada.",
        "¡Adiós a las alturas de {actor}! Pierde {delta} puestos sin remedio.",
    ],
    ("big_down", "sarcastico"): [
        "Vaya por Dios. {actor} pierde {delta} puestos. Quién lo iba a decir.",
        "Sorpresa: {actor} se ha desinflado. -{delta} posiciones.",
        "{actor} -{delta}. Bueno, mejor no comentar.",
        "Pues {actor} ahí, perdiendo {delta} puestos. Era previsible.",
        "Mira tú dónde estaba {actor} y dónde está ahora. {delta} puestos menos.",
    ],
    ("big_down", "meme"): [
        "{actor} -{delta}. F.",
        "{actor} pierde {delta} puestos. Speedrun al fondo any%.",
        "POV: eres {actor} y has bajado {delta} puestos. 🥲",
        "{actor} desbloqueando logro: pérdida masiva. -{delta} pos.",
        "{actor} -{delta}. El bote te dice adiós con la manita.",
    ],
    ("big_down", "residuos"): [
        "{actor} directo a la balsa de lixiviados. -{delta} puestos.",
        "Sellado de bentonita sobre {actor}: cae {delta} posiciones.",
        "{actor} -{delta}. Material no inertizable.",
        "Esto no se composta. {actor} al vertedero, {delta} puestos abajo.",
        "Bidón con fuga para {actor}: -{delta} en la tabla.",
        "Inspección sorpresa: {actor} -{delta} por incumplimientos.",
        "Sanción firme a {actor}: -{delta} puestos preventivos.",
        "Factura impagada: {actor} cae {delta} puestos hasta saldar.",
    ],

    # ============= NEW_LEADER (cambio de #1) =============
    ("new_leader", "cuñao"): [
        "¡Atención al nuevo jefe! {actor} le pisa el podio a {prev_leader}.",
        "Hala, ha cambiado el cabeza de mesa. Ahora manda {actor}, {prev_leader} a comer aparte.",
        "{actor} jefe nuevo. {prev_leader}, hala, a hacer cola.",
        "¡Cambio en la cabeza! {actor} arrebata el liderato a {prev_leader}.",
    ],
    ("new_leader", "cronista"): [
        "¡Y SE HACE CON EL LIDERATO! {actor} desbanca a {prev_leader} en la cabeza de la tabla.",
        "¡Histórico! {actor} arrebata el primer puesto a {prev_leader}.",
        "Atención que esto no se ve todos los días: {actor} líder, {prev_leader} a segundo.",
        "¡Nuevo número uno! {actor} firma su mejor jornada, {prev_leader} queda relegado.",
    ],
    ("new_leader", "sarcastico"): [
        "Pues mira, {actor} ahora es líder. {prev_leader}, a hacer cola.",
        "Cosas que pasan: {actor} líder. {prev_leader} se queda con cara de bobo.",
        "{actor} primero. {prev_leader} segundo. Quién lo iba a decir.",
        "Sorpresa moderada: {actor} es nuevo líder. {prev_leader}, tira para abajo.",
    ],
    ("new_leader", "meme"): [
        "{actor} nuevo líder. {prev_leader} en shock. 👑",
        "POV: eras {prev_leader} y ahora {actor} está donde estabas tú.",
        "{actor} desbanca a {prev_leader}: rey destronado.",
        "{actor} entra al chat como líder. {prev_leader} ha salido.",
    ],
    ("new_leader", "residuos"): [
        "Cambio de capataz: {actor} se hace con la cuadrilla, {prev_leader} releva turno.",
        "{actor} gana la licitación esta semana. {prev_leader}, recurso desestimado.",
        "Nuevo jefe de planta: {actor}. {prev_leader}, a mantenimiento.",
        "{actor} firma el contrato gordo. {prev_leader}, a tramitar deuda.",
        "Concesión municipal para {actor}. {prev_leader}, contrato no renovado.",
    ],

    # ============= NEW_LAST (nuevo farolillo) =============
    ("new_last", "cuñao"): [
        "Hala, {actor}, ahora el farolillo lo llevas tú. {prev_last} respira tranquilo.",
        "{prev_last} pasa el testigo a {actor}: bienvenido al sótano.",
        "{actor}, tranquilo, esto le pasa a cualquiera. Pero le pasa a ti.",
    ],
    ("new_last", "cronista"): [
        "Y por el final de la tabla aparece {actor}, relevando a {prev_last} en el farolillo rojo.",
        "Cambio en el sótano: {actor} pasa a ser el último, {prev_last} consigue salir del fondo.",
        "{actor} hereda el último puesto de {prev_last}. ¡Bienvenido al club!",
    ],
    ("new_last", "sarcastico"): [
        "Enhorabuena {actor}, eres el nuevo último. {prev_last} te lo agradece.",
        "{actor}, has llegado a lo más bajo. {prev_last} ya respira.",
        "Pues {actor} ya tiene su pegatina de farolillo rojo. {prev_last}, libre.",
    ],
    ("new_last", "meme"): [
        "{actor} ahora último. {prev_last} liberado. 🦴",
        "Cambio de farolillo: {actor} entra, {prev_last} sale.",
        "{actor} desbloquea: 'Eres el último'. Achievement unlocked.",
    ],
    ("new_last", "residuos"): [
        "{actor} al rechazo. {prev_last} pasa a tratamiento.",
        "{actor} hereda el cubo gris de {prev_last}. Bienvenido al fondo.",
        "Material no separado: {actor} al fondo. {prev_last}, has subido al amarillo.",
        "{actor}, bidón con fuga heredado de {prev_last}.",
    ],

    # ============= DEBUT (primera vez en el ranking) =============
    ("debut", "cuñao"): [
        "¡Ojo, que ha llegado {actor}! Bienvenido al barro.",
        "{actor} estrena ranking. ¡Suerte, chaval!",
        "Nuevo en la sala: {actor}. A ver qué tal se le da.",
    ],
    ("debut", "cronista"): [
        "¡Recibimos a {actor} en la clasificación! Aquí empieza su historia.",
        "Hace su aparición {actor}. La afición espera grandes cosas.",
        "{actor} entra en escena. Todos los ojos puestos en su debut.",
    ],
    ("debut", "sarcastico"): [
        "Bienvenido, {actor}. Disfruta mientras puedas.",
        "Estrena ranking {actor}. Veremos cuánto le dura la sonrisa.",
        "{actor} se une a la fiesta. Tarde, pero se une.",
    ],
    ("debut", "meme"): [
        "{actor} ha entrado al chat. 🎮",
        "Nuevo participante: {actor}. Tutorial en marcha.",
        "{actor} unlocked: clasificación.",
    ],
    ("debut", "residuos"): [
        "{actor} entra en planta. Primera pesada en báscula.",
        "Alta nueva: {actor}. DCS pendiente de tramitar.",
        "Nuevo cliente: {actor}. Contrato de tratamiento por firmar.",
    ],
}


# ---------- Helpers ----------

def load_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def rankings_equal(a, b):
    if len(a) != len(b):
        return False
    return {e["name"]: e.get("total", 0) for e in a} == {e["name"]: e.get("total", 0) for e in b}


def pick_personaje(weights):
    """Elige un personaje segun los pesos por sala."""
    pers = list(weights.keys())
    pesos = [weights[p] for p in pers]
    if sum(pesos) == 0:
        return random.choice(pers)
    return random.choices(pers, weights=pesos, k=1)[0]


def join_es(names):
    """Junta una lista de nombres con 'y' al final, estilo español."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} y {names[1]}"
    return ", ".join(names[:-1]) + f" y {names[-1]}"


def first_name(full_name):
    """Devuelve solo el primer nombre — los apellidos a veces hacen frases pesadas."""
    return str(full_name).split()[0] if full_name else "—"


def format_template(template, event):
    actor = first_name(event.get("actor"))
    victim = join_es([first_name(v) for v in event.get("victims", [])]) if event.get("victims") else ""
    prev_leader = first_name(event.get("previous_leader", ""))
    prev_last = first_name(event.get("previous_last", ""))
    delta = event.get("delta", 0)
    return template.format(actor=actor, victim=victim, prev_leader=prev_leader, prev_last=prev_last, delta=delta)


def generate_phrase(event, room_weights, recent_phrases):
    """Genera una frase para un evento, evitando repeticion con las recientes."""
    etype = event["type"]
    # Hasta 10 intentos para evitar repeticion. Si todos repiten, devolvemos la ultima.
    for _ in range(10):
        personaje = pick_personaje(room_weights)
        templates = T.get((etype, personaje))
        if not templates:
            # Si no hay templates para ese personaje, prueba con cronista (siempre disponible)
            templates = T.get((etype, "cronista"))
        if not templates:
            continue
        template = random.choice(templates)
        text = format_template(template, event)
        closer = random.choice(CLOSERS)
        if closer:
            text = f"{text} {closer}"
        if text not in recent_phrases:
            return text, personaje
    return text, personaje


# ---------- Event detection ----------

def detect_events(current_ranking, last_seen_ranking):
    """Compara current vs last_seen y devuelve eventos detectados."""
    last_pos = {e["name"]: i + 1 for i, e in enumerate(last_seen_ranking)}
    new_pos = {e["name"]: i + 1 for i, e in enumerate(current_ranking)}
    last_names = set(last_pos.keys())
    new_names = set(new_pos.keys())

    events = []

    # Debuts
    debuts = new_names - last_names
    for name in sorted(debuts):
        events.append({"type": "debut", "actor": name})

    # Cambio de lider
    if last_seen_ranking and current_ranking:
        old_leader = last_seen_ranking[0]["name"]
        new_leader = current_ranking[0]["name"]
        if old_leader != new_leader and new_leader not in debuts:
            events.append({"type": "new_leader", "actor": new_leader, "previous_leader": old_leader})

    # Cambio de farolillo
    if last_seen_ranking and current_ranking and len(current_ranking) >= 4:
        old_last = last_seen_ranking[-1]["name"]
        new_last = current_ranking[-1]["name"]
        if old_last != new_last and new_last not in debuts:
            events.append({"type": "new_last", "actor": new_last, "previous_last": old_last})

    # Subidas/caidas/adelantamientos
    for entry in current_ranking:
        n = entry["name"]
        if n in debuts:
            continue
        ppos = last_pos.get(n)
        if ppos is None:
            continue
        npos = new_pos[n]
        delta = ppos - npos  # positivo si subio

        if delta >= 3:
            events.append({"type": "big_up", "actor": n, "delta": delta})
        elif delta <= -3:
            events.append({"type": "big_down", "actor": n, "delta": -delta})

        # Sorpassos: quien estaba delante de mi y ahora detras
        if delta > 0:
            victims = []
            for other in current_ranking:
                on = other["name"]
                if on == n or on in debuts:
                    continue
                op = last_pos.get(on)
                if op is None:
                    continue
                if op < ppos and new_pos[on] > npos:
                    victims.append(on)
            if victims:
                events.append({"type": "overtake", "actor": n, "victims": victims})

    return events


# ---------- Per-room feed update ----------

def update_feed_for_room(room_label, scoreboard_path, feed_path, weights_key):
    """Genera eventos para un scoreboard y los anade al feed correspondiente."""
    scoreboard = load_json(scoreboard_path, {})
    current = scoreboard.get("ranking", [])
    if not current:
        return 0

    feed = load_json(feed_path, {"events": [], "last_seen_ranking": []})
    if not isinstance(feed.get("events"), list):
        feed["events"] = []
    last_seen = feed.get("last_seen_ranking", [])

    if rankings_equal(current, last_seen):
        return 0

    events = detect_events(current, last_seen)
    if not events:
        # Actualizamos last_seen igualmente para que el siguiente diff sea limpio
        feed["last_seen_ranking"] = current
        feed["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_json(feed_path, feed)
        return 0

    # Generar frases
    weights = PERSONAJE_WEIGHTS.get(weights_key) or PERSONAJE_WEIGHTS["_default"]
    recent = [e.get("text", "") for e in feed["events"][:ANTI_REPEAT_WINDOW]]
    now = datetime.now(timezone.utc).isoformat()
    new_events = []
    for ev in events:
        text, personaje = generate_phrase(ev, weights, recent)
        recent.insert(0, text)
        new_events.append({
            "ts": now,
            "type": ev["type"],
            "actor": ev.get("actor"),
            "victims": ev.get("victims") or [],
            "previous_leader": ev.get("previous_leader"),
            "previous_last": ev.get("previous_last"),
            "delta": ev.get("delta"),
            "personaje": personaje,
            "text": text,
        })

    # Prepend (mas recientes arriba) + cap
    feed["events"] = new_events + feed["events"]
    feed["events"] = feed["events"][:MAX_EVENTS]
    feed["last_seen_ranking"] = current
    feed["last_updated"] = now
    feed["room"] = room_label

    save_json(feed_path, feed)
    return len(new_events)


def main():
    FEED_DIR.mkdir(parents=True, exist_ok=True)

    # Bolilla: por sala
    rooms_doc = load_json(ROOMS_FILE, {"rooms": []})
    for room in rooms_doc.get("rooms", []):
        rid = room["id"]
        rname = room.get("name", rid)
        sb_path = DATA / "rooms" / rid / "scoreboard.json"
        feed_path = FEED_DIR / f"{rid}.json"
        n = update_feed_for_room(rname, sb_path, feed_path, rid)
        print(f"[feed] sala '{rid}': {n} eventos nuevos")

    # Porra
    feed_porra = FEED_DIR / "porra.json"
    n = update_feed_for_room("Porra", PORRA_SCOREBOARD, feed_porra, "_porra")
    print(f"[feed] porra: {n} eventos nuevos")


if __name__ == "__main__":
    main()

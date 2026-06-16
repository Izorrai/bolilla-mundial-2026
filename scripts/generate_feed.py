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
# Cuantas frases recientes no se repiten (mas alto = menos repeticion visible)
ANTI_REPEAT_WINDOW = 35

# Salas donde generamos feed. El resto no recibe bromas (la pe\xf1a del barrio
# no aprecia el humor de oficina, ni la porra entre 7 amigos).
BOT_ENABLED_ROOMS = {"grupoagaleus"}
BOT_ENABLED_PORRA = False


# ---------- Personajes y reparto por sala ----------
# GrupoAgaleus va 100% al personaje "residuos" (curro / gestion de residuos),
# que es donde encaja el humor que les hace gracia.
PERSONAJE_WEIGHTS = {
    "grupoagaleus":  {"residuos": 100},
    "_default":      {"residuos": 100},
}

CLOSERS = ["", "", "", "", "", "", "🗑️", "📦", "⚖️", "📊", "🚛", "📋", "💼", "📑", "🔧"]


# ---------- Plantillas ----------
# Slots: {actor}, {victim}, {delta}, {prev_leader}, {prev_last}
# Cuanto mayor el banco, menos repeticion. Empiezo con ~6-8 por personaje/evento.

T = {
    # ============= DEBUT (primera vez en el ranking) =============
    # Esta es la que mas dispara en arranques (28 altas), asi que mas variedad.
    ("debut", "residuos"): [
        "Alta nueva: {actor}. DCS pendiente de tramitar.",
        "Nuevo cliente: {actor}. Contrato de tratamiento por firmar.",
        "{actor} entra en planta. Primera pesada en báscula.",
        "{actor} se incorpora a la cartera. Comercial avisado.",
        "Nuevo en la cartera: {actor}. Tarifa estándar aplicada.",
        "{actor} se da de alta. Asignar código CER y a tramitar.",
        "Alta provisional: {actor}. 15 días para completar trámites.",
        "Recepción de {actor}. Albarán generado.",
        "{actor} firma alta. Cuestión de tiempo que pase por báscula.",
        "Llega {actor}. Inscripción en el registro de productores.",
        "Cliente nuevo: {actor}. Visita técnica programada.",
        "{actor} entra en producción. Caracterización del residuo pendiente.",
        "Nueva entrada: {actor}. Falta validar productor.",
        "{actor} presenta documentación. Periodo de evaluación abierto.",
        "Alta tramitada: {actor}. Asignar gestor de cuenta.",
        "{actor} estrena ficha de cliente. Esperando primer servicio.",
        "{actor} llega a almacén. Asignar contenedor según CER.",
        "Llegada de {actor}. Pendiente de homologación.",
        "{actor}, alta en sistema. Pendiente de firma del responsable.",
        "Nuevo expediente: {actor}. Pasa a operaciones.",
        "{actor} se incorpora. Plantilla revisada, equipo asignado.",
        "Alta de {actor}. Periodo de prueba: 30 días.",
        "{actor} entra al circuito. Producción pendiente de cuantificar.",
        "Cliente nuevo: {actor}. Pendiente visita técnica del comercial.",
        "{actor} firma compromiso. A esperar primer albarán de retirada.",
        "Alta operativa de {actor}. Esperando primera recogida.",
        "{actor} se ha dado de alta. Asignar ruta a la cisterna.",
        "Nuevo registro: {actor}. Documento de Identificación en trámite.",
        "{actor} entra en planta. Visto bueno del jefe de producción.",
        "Alta confirmada: {actor}. Bienvenido a la facturación mensual.",
        "{actor} se da de alta. Esperando confirmación de Hacienda.",
        "Cliente potencial validado: {actor}. Pasa a productor activo.",
        "{actor}, alta provisional. Falta análisis del CER 200301.",
        "Recepción de {actor}. A coger turno en el muelle de descarga.",
        "{actor} se incorpora. Pendiente formación PRL.",
    ],

    # ============= OVERTAKE (adelantamiento) =============
    ("overtake", "residuos"): [
        "{actor} firma el albarán de retirada antes que {victim}. Recogida pendiente para {victim}.",
        "Cliente VIP para {actor}: contrato sellado. {victim} pasa a tarifa estándar.",
        "{actor} tramita el DCS en cinco minutos. {victim} todavía persiguiendo firma del productor.",
        "{actor} cobra factura del mes. {victim} reclamando segundo aviso.",
        "{actor} gana la licitación. {victim} se queda con la propuesta no aceptada.",
        "Pase de báscula limpio para {actor}. Tara mal declarada de {victim}.",
        "{actor} presenta certificado ISO en regla. {victim} caducado.",
        "{actor} tramita CER al primer intento. {victim} devuelto a producción.",
        "Visita comercial cerrada para {actor}. {victim} reagenda.",
        "Cliente histórico se queda con {actor}. {victim} pierde la cartera.",
        "{actor} pasa el control de calidad. {victim} se queda en cuarentena.",
        "{actor} carga camión completo. {victim} esperando segunda vuelta.",
        "{actor} tiene la planta a punto. {victim} con avería en el compactador.",
        "{actor} presenta memoria anual. {victim} todavía redactando.",
        "{actor} liquida pago a 30 días. {victim} con factura impagada.",
        "{actor} cierra el cuadre del mes. {victim} todavía con descuadres.",
        "Trasvase de cisterna sin incidencias para {actor}. {victim} a limpieza interna.",
        "{actor} pasa la inspección sin observaciones. {victim} con periodo de subsanación.",
        "DCS sellado de {actor}. {victim} esperando respuesta del productor.",
        "{actor} compacta a {victim} como palet de cartón. Bala lista.",
        "{actor} aplica inertización. {victim} a la balsa de lixiviados.",
        "{actor} factura el adelantamiento. {victim}, vencimiento ya.",
        "{actor} libera muelle. {victim} cola para descarga.",
        "Permiso ambiental renovado para {actor}. {victim} pendiente de Junta.",
        "{actor} cierra ofertas del trimestre. {victim} sin presupuesto enviado.",
        "{actor} entrega el lote en plazo. {victim} con incumplimiento de servicio.",
        "{actor} consigue homologación. {victim} sigue con muestras en laboratorio.",
        "Comercial de {actor} firma contrato. Comercial de {victim}, otro café.",
        "{actor} liquida albarán pendiente. {victim} a regularizar saldo.",
        "{actor} tramita Documento de Identificación. {victim} en periodo de subsanación.",
    ],

    # ============= BIG_UP (subida >=3) =============
    ("big_up", "residuos"): [
        "Pase de báscula exitoso: {actor} +{delta} toneladas de puntos.",
        "{actor} gana 3 licitaciones en una semana. +{delta} puestos.",
        "Auditoría ISO superada sin objeciones: {actor} +{delta}.",
        "Camión cargado y facturado el mismo día: {actor} +{delta}.",
        "Nuevo cliente para {actor}. Cartera ampliada. +{delta}.",
        "{actor} presenta informe anual antes de plazo. Inspector contento. +{delta}.",
        "Cobro adelantado de facturación: {actor} +{delta} puestos.",
        "{actor} consigue homologación nueva. +{delta}.",
        "Liquidación de comisiones favorable: {actor} +{delta}.",
        "Cierre contable sin descuadres: {actor} +{delta}.",
        "{actor} pasa la inspección ambiental sin sanción. +{delta}.",
        "Trasvase de cisterna récord: {actor} +{delta}.",
        "{actor} consigue contrato marco con la administración. +{delta}.",
        "{actor} cierra cartera completa del mes. +{delta} puestos.",
        "{actor} pasa el CER al primer intento. +{delta}.",
        "Compactador a pleno rendimiento: {actor} +{delta} balas, +{delta} puestos.",
        "Recogida selectiva del año: {actor} +{delta}.",
        "{actor} consigue renovación del permiso ambiental. +{delta}.",
        "Inertización exitosa de lote completo: {actor} +{delta}.",
        "{actor} factura por encima del objetivo trimestral. +{delta}.",
        "Visita del inspector cerrada sin acta: {actor} +{delta}.",
        "{actor} cobra deuda histórica pendiente. +{delta}.",
        "{actor} cierra ronda comercial: {delta} contratos nuevos. +{delta} puestos.",
        "Pase limpio en báscula sin tara errónea: {actor} +{delta}.",
        "Bonificación de cumplimiento ambiental para {actor}. +{delta}.",
    ],

    # ============= BIG_DOWN (caida >=3) =============
    ("big_down", "residuos"): [
        "Auditoría con incidencias para {actor}: -{delta} puestos.",
        "Factura impagada del mes: {actor} -{delta} puestos.",
        "Camión averiado en ruta: {actor} -{delta}.",
        "Cliente reclama: {actor} -{delta} hasta resolver.",
        "Cisterna con fuga en almacén: {actor} -{delta}.",
        "Inspección sorpresa con incumplimientos: {actor} -{delta}.",
        "{actor} olvida tramitar DCS de la semana: -{delta}.",
        "Cierre de mes con descuadre: {actor} -{delta}.",
        "Sanción administrativa firme: {actor} -{delta} puestos preventivos.",
        "Cliente histórico se va a la competencia: {actor} -{delta}.",
        "Báscula descalibrada para {actor}: -{delta} en la liquidación.",
        "Albarán perdido en producción: {actor} -{delta}.",
        "Conductor de baja médica: {actor} paralizado, -{delta}.",
        "{actor} no llega al objetivo del trimestre: -{delta}.",
        "Devolución de factura por error: {actor} -{delta}.",
        "Vertido fuera de autorización: {actor} -{delta}.",
        "Bidón con fuga olvidado en almacén: {actor} -{delta}.",
        "{actor} pierde la licitación. -{delta} puestos.",
        "Multa por exceso de stock no autorizado: {actor} -{delta}.",
        "Servicio rechazado por el cliente: {actor} -{delta}.",
        "Compactador averiado: {actor} para tres días, -{delta}.",
        "Sellado de bentonita sobre {actor}: -{delta}, directo al fondo.",
        "Falta documentación ADR: ruta paralizada. {actor} -{delta}.",
        "{actor} pierde permiso ambiental: -{delta} hasta renovación.",
        "Reclamación judicial de cliente: {actor} -{delta} puestos preventivos.",
    ],

    # ============= NEW_LEADER =============
    ("new_leader", "residuos"): [
        "Cambio de capataz: {actor} se hace con la cuadrilla. {prev_leader}, a relevo de turno.",
        "{actor} gana la licitación esta semana. {prev_leader}, recurso desestimado.",
        "Nuevo jefe de planta de tratamiento: {actor}. {prev_leader}, a mantenimiento.",
        "{actor} firma el contrato gordo con la administración. {prev_leader}, a tramitar deuda pendiente.",
        "Concesión municipal para {actor}. {prev_leader}, contrato no renovado.",
        "{actor} factura más que toda la sala junta. {prev_leader} al segundo puesto.",
        "Renovación de licencia ambiental para {actor}. {prev_leader} caducada.",
        "{actor} consigue ISO 14001. {prev_leader} en periodo de adaptación.",
        "Inspector certifica planta de {actor}. {prev_leader} bajo revisión.",
        "Nuevo gestor autorizado: {actor}. {prev_leader} pierde la categoría.",
        "{actor} se queda con el contrato marco. {prev_leader}, a buscar nuevos clientes.",
        "Bono anual de gestión para {actor}. {prev_leader}, descuento por incumplimiento.",
        "{actor} cierra balance positivo del año. {prev_leader} en pérdidas.",
        "Adjudicación directa a {actor}. {prev_leader}, propuesta archivada.",
    ],

    # ============= NEW_LAST (nuevo farolillo) =============
    ("new_last", "residuos"): [
        "{actor} al rechazo. {prev_last} pasa a tratamiento.",
        "{actor} hereda el cubo gris de {prev_last}. Bienvenido al fondo.",
        "Material no separado: {actor} al fondo. {prev_last}, has subido al amarillo.",
        "{actor}, bidón con fuga heredado de {prev_last}.",
        "Factura impagada para {actor}. {prev_last}, al fin liquidaste.",
        "Inspección pendiente para {actor}. {prev_last}, periodo cerrado.",
        "{actor} se queda con la deuda. {prev_last}, libre de cargas.",
        "{actor}, contrato no renovado. {prev_last} consigue prórroga.",
        "{actor} pasa a estado de subsanación. {prev_last}, sale del trámite.",
        "{actor} olvidó la auditoría. {prev_last} ya pasó la suya.",
        "{actor}, código CER por confirmar. {prev_last} aclarado.",
        "{actor} hereda el expediente sancionador de {prev_last}.",
        "{actor} al periodo de prueba forzoso. {prev_last} renovado.",
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

    # Solo procesamos las salas habilitadas (oficina). El resto no recibe feed.
    rooms_doc = load_json(ROOMS_FILE, {"rooms": []})
    for room in rooms_doc.get("rooms", []):
        rid = room["id"]
        if rid not in BOT_ENABLED_ROOMS:
            print(f"[feed] sala '{rid}': bot deshabilitado")
            continue
        rname = room.get("name", rid)
        sb_path = DATA / "rooms" / rid / "scoreboard.json"
        feed_path = FEED_DIR / f"{rid}.json"
        n = update_feed_for_room(rname, sb_path, feed_path, rid)
        print(f"[feed] sala '{rid}': {n} eventos nuevos")

    if BOT_ENABLED_PORRA:
        feed_porra = FEED_DIR / "porra.json"
        n = update_feed_for_room("Porra", PORRA_SCOREBOARD, feed_porra, "_porra")
        print(f"[feed] porra: {n} eventos nuevos")
    else:
        print("[feed] porra: bot deshabilitado")


if __name__ == "__main__":
    main()

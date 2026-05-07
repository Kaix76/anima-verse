"""Welt-Daten: Orte, Raeume und Aktivitaeten (User-Level)

Orte und ihre Raeume werden pro User gespeichert in:
  storage/users/{username}/world.json

Jeder Ort hat eine persistente ID (8-Zeichen Hex), damit Umbenennungen
keine Referenzen in Character-Profilen, Schedulern etc. zerstoeren.

Jeder Ort hat Raeume (rooms) mit Name, Beschreibung und Aktivitaeten.
Aktivitaeten sind als Objekte {name, description} in den Raeumen eingebettet.
Galerie-Bilder werden Raeumen zugeordnet (statt direkt Aktivitaeten).
"""
import json
import random as _random
import re
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("world")

from app.core.paths import get_storage_dir


def _get_world_file() -> Path:
    """Gibt den Pfad zur world.json zurueck."""
    sd = get_storage_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "world.json"


def _migrate_room_image_prompts(data: Dict[str, Any]) -> bool:
    """Migriert Room image_prompt -> image_prompt_day (einmalig beim Laden).

    Returns True wenn Daten geaendert wurden.
    """
    changed = False
    for loc in data.get("locations", []):
        for room in loc.get("rooms", []):
            if "image_prompt" in room and "image_prompt_day" not in room:
                room["image_prompt_day"] = room.pop("image_prompt")
                changed = True
            if "image_prompt_night" not in room:
                room["image_prompt_night"] = ""
                changed = True
    return changed


def _load_world_data() -> Dict[str, Any]:
    """Laedt die Weltdaten aus der DB (Locations + ihre Raeume).

    Locations werden als vollstaendige Dicts aus dem meta-Blob geladen.
    Raeume sind eingebettet in locations.meta.rooms.
    Fallback auf world.json wenn DB leer oder fehlerhaft.
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, name, description, grid_x, grid_y, outfit_type, "
            "image_prompt_day, image_prompt_night, image_prompt_map, "
            "visible_when, accessible_when, background_images, meta "
            "FROM locations ORDER BY name ASC"
        ).fetchall()
        if rows:
            locations = []
            for r in rows:
                meta = {}
                try:
                    meta = json.loads(r[12] or "{}")
                except Exception:
                    pass
                if meta and "id" in meta:
                    # Vollstaendiges Location-Dict aus meta
                    loc = meta
                else:
                    # Reconstruct from columns
                    loc = {
                        "id": r[0],
                        "name": r[1] or "",
                        "description": r[2] or "",
                        "grid_x": r[3],
                        "grid_y": r[4],
                        "outfit_type": r[5] or "",
                        "image_prompt_day": r[6] or "",
                        "image_prompt_night": r[7] or "",
                        "image_prompt_map": r[8] or "",
                        "rooms": [],
                    }
                    try:
                        loc["visible_when"] = json.loads(r[9] or "[]")
                    except Exception:
                        loc["visible_when"] = []
                    try:
                        loc["accessible_when"] = json.loads(r[10] or "[]")
                    except Exception:
                        loc["accessible_when"] = []
                    try:
                        loc["background_images"] = json.loads(r[11] or "[]")
                    except Exception:
                        loc["background_images"] = []
                    loc.update(meta)

                    # Load rooms from rooms table
                    room_rows = conn.execute(
                        "SELECT id, name, outfit_type, meta FROM rooms "
                        "WHERE location_id=? ORDER BY rowid ASC",
                        (r[0],),
                    ).fetchall()
                    rooms = []
                    for rr in room_rows:
                        rmeta = {}
                        try:
                            rmeta = json.loads(rr[3] or "{}")
                        except Exception:
                            pass
                        if rmeta and "id" in rmeta:
                            rooms.append(rmeta)
                        else:
                            rooms.append({
                                "id": rr[0],
                                "name": rr[1] or "",
                                "outfit_type": rr[2] or "",
                                "description": "",
                                "activities": [],
                                **rmeta,
                            })
                    loc["rooms"] = rooms
                locations.append(loc)
            data = {"locations": locations}
            _migrate_room_image_prompts(data)
            return data
    except Exception as e:
        logger.warning("_load_world_data DB-Fehler: %s", e)

    # Fallback: JSON-Datei
    path = _get_world_file()
    if path.exists():
        try:
            with _world_file_lock:
                data = json.loads(path.read_text(encoding="utf-8"))
                if _migrate_room_image_prompts(data):
                    path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    logger.info("Room image_prompt -> image_prompt_day migriert")
            return data
        except Exception:
            pass
    return {"locations": []}


_world_file_lock = threading.Lock()


def _save_world_data(data: Dict[str, Any]):
    """Speichert die Weltdaten in die DB (Locations + Raeume als Upsert)."""
    now = __import__('datetime').datetime.now().isoformat()
    locations = data.get("locations", [])
    try:
        with transaction() as conn:
            existing_loc_ids = {r[0] for r in conn.execute(
                "SELECT id FROM locations"
            ).fetchall()}
            new_loc_ids = {loc.get("id") for loc in locations if loc.get("id")}

            for lid in existing_loc_ids - new_loc_ids:
                conn.execute("DELETE FROM locations WHERE id=?", (lid,))

            for loc in locations:
                lid = loc.get("id")
                if not lid:
                    continue
                conn.execute("""
                    INSERT INTO locations
                        (id, name, description, grid_x, grid_y, outfit_type,
                         image_prompt_day, image_prompt_night, image_prompt_map,
                         visible_when, accessible_when, background_images, meta,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        description=excluded.description,
                        grid_x=excluded.grid_x,
                        grid_y=excluded.grid_y,
                        outfit_type=excluded.outfit_type,
                        image_prompt_day=excluded.image_prompt_day,
                        image_prompt_night=excluded.image_prompt_night,
                        image_prompt_map=excluded.image_prompt_map,
                        visible_when=excluded.visible_when,
                        accessible_when=excluded.accessible_when,
                        background_images=excluded.background_images,
                        meta=excluded.meta,
                        updated_at=excluded.updated_at
                """, (
                    lid,
                    loc.get("name", ""),
                    loc.get("description", ""),
                    loc.get("grid_x"),
                    loc.get("grid_y"),
                    loc.get("outfit_type", ""),
                    loc.get("image_prompt_day", ""),
                    loc.get("image_prompt_night", ""),
                    loc.get("image_prompt_map", ""),
                    json.dumps(loc.get("visible_when", []), ensure_ascii=False),
                    json.dumps(loc.get("accessible_when", []), ensure_ascii=False),
                    json.dumps(loc.get("background_images", []), ensure_ascii=False),
                    json.dumps(loc, ensure_ascii=False),
                    now,
                    now,
                ))

                # Upsert rooms
                rooms = loc.get("rooms", [])
                existing_room_ids = {r[0] for r in conn.execute(
                    "SELECT id FROM rooms WHERE location_id=?", (lid,)
                ).fetchall()}
                new_room_ids = {r.get("id") for r in rooms if r.get("id")}
                for rid in existing_room_ids - new_room_ids:
                    conn.execute("DELETE FROM rooms WHERE id=?", (rid,))

                for room in rooms:
                    rid = room.get("id")
                    if not rid:
                        continue
                    conn.execute("""
                        INSERT INTO rooms (id, location_id, name, outfit_type, meta)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            location_id=excluded.location_id,
                            name=excluded.name,
                            outfit_type=excluded.outfit_type,
                            meta=excluded.meta
                    """, (
                        rid,
                        lid,
                        room.get("name", ""),
                        room.get("outfit_type", ""),
                        json.dumps(room, ensure_ascii=False),
                    ))
    except Exception as e:
        logger.error("_save_world_data DB-Fehler: %s", e)


# === Orte ===

def _generate_location_id() -> str:
    """Generiert eine eindeutige 8-Zeichen Hex-ID fuer einen Ort."""
    return uuid.uuid4().hex[:8]


def _generate_room_id() -> str:
    """Generiert eine eindeutige 8-Zeichen Hex-ID fuer einen Raum."""
    return uuid.uuid4().hex[:8]


# === Raum-Hilfsfunktionen ===

def get_location_rooms(location: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gibt die Raeume eines Orts zurueck."""
    return location.get("rooms", [])


def get_room_by_id(location: Dict[str, Any], room_id: str) -> Optional[Dict[str, Any]]:
    """Findet einen Raum per ID in einem Ort."""
    if not room_id:
        return None
    for room in location.get("rooms", []):
        if room.get("id") == room_id:
            return room
    return None


def get_room_by_name(location: Dict[str, Any], room_name: str) -> Optional[Dict[str, Any]]:
    """Findet einen Raum per Name (exakt oder fuzzy) in einem Ort."""
    if not room_name:
        return None
    rooms = location.get("rooms", [])
    name_lower = room_name.lower()
    # Exakter Match
    for room in rooms:
        if room.get("name", "").lower() == name_lower:
            return room
    # Substring Match
    for room in rooms:
        rn = room.get("name", "").lower()
        if rn and (rn in name_lower or name_lower in rn):
            return room
    # Wort-basierter Match: alle Wörter des kürzeren im längeren enthalten
    # z.B. "Private Büro" matched "Privates Büro" (büro in beiden, privat* in beiden)
    query_words = name_lower.split()
    for room in rooms:
        rn = room.get("name", "").lower()
        if not rn:
            continue
        room_words = rn.split()
        # Prüfe ob jedes Query-Wort als Prefix eines Raum-Worts vorkommt (oder umgekehrt)
        if query_words and room_words and all(
            any(qw.startswith(rw) or rw.startswith(qw) for rw in room_words)
            for qw in query_words
        ):
            return room
    return None


def find_room_by_activity(location: Dict[str, Any], activity_name: str) -> Optional[Dict[str, Any]]:
    """Findet den Raum, der eine bestimmte Aktivitaet enthaelt."""
    if not activity_name:
        return None
    act_lower = activity_name.lower()
    for room in location.get("rooms", []):
        for act in room.get("activities", []):
            name = act.get("name", "") if isinstance(act, dict) else str(act)
            if name.lower() == act_lower:
                return room
    # Fuzzy
    for room in location.get("rooms", []):
        for act in room.get("activities", []):
            name = act.get("name", "") if isinstance(act, dict) else str(act)
            if name.lower() and (name.lower() in act_lower or act_lower in name.lower()):
                return room
    return None


def _validate_room_description(text: str) -> str:
    """Letzte Sicherheitspruefung bevor eine Raum-Beschreibung gespeichert wird.

    Lehnt Texte ab die offensichtlich keine Raum-Beschreibungen sind
    (eingebettete JSON-Objekte, Tool-Call-Tags, Appearance-Daten).
    """
    if not text or not text.strip():
        return text
    stripped = text.strip()
    # JSON-Objekte (halluzinierte Tool-Calls)
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped) if stripped.endswith("}") else None
            if isinstance(parsed, dict) and ("location_id" in parsed or "room" in parsed):
                logger.warning("Raum-Beschreibung ist JSON-Objekt — abgelehnt")
                return ""
        except Exception:
            pass
        # JSON-Praefix gefolgt von anderem Text
        if '}\n' in stripped or '}<' in stripped:
            logger.warning("Raum-Beschreibung enthaelt JSON-Praefix — abgelehnt")
            return ""
    # Tool-Call-Tags
    if re.search(r'<tool\s+name=', stripped):
        logger.warning("Raum-Beschreibung enthaelt Tool-Tags — abgelehnt")
        return ""
    # Appearance-Daten (physische Character-Beschreibungen)
    appearance_hits = sum(1 for p in [
        r'\b\d+\s*years?\s*(young|old)\b',
        r'\b(large|small|round|perfect)\s+(breasts?|butt|chest)\b',
        r'\b(short|tall|athletic|slim)\s+(frame|build|body)\b',
    ] if re.search(p, stripped, re.IGNORECASE))
    if appearance_hits >= 2:
        logger.warning("Raum-Beschreibung enthaelt Appearance-Daten — abgelehnt")
        return ""
    return text


def add_room(location_id: str, room_name: str, description: str = "",
             image_prompt_day: str = "", image_prompt_night: str = "") -> Optional[Dict[str, Any]]:
    """Fuegt einen neuen Raum zu einem Ort hinzu. Gibt den Raum zurueck oder None bei Fehler."""
    # Validierung
    description = _validate_room_description(description)
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            rooms = loc.setdefault("rooms", [])
            # Duplikat-Check (case-insensitive)
            if any(r.get("name", "").lower() == room_name.lower() for r in rooms):
                logger.warning("Raum '%s' existiert bereits in Location %s", room_name, location_id)
                return None
            new_room = {
                "id": _generate_room_id(),
                "name": room_name,
                "description": description,
                "image_prompt_day": image_prompt_day,
                "image_prompt_night": image_prompt_night,
                "activities": [],
            }
            if image_prompt_day or image_prompt_night:
                new_room["prompt_changed"] = True
            rooms.append(new_room)
            _save_world_data(data)
            logger.info("Raum '%s' hinzugefuegt zu Location %s (id=%s)", room_name, location_id, new_room["id"])
            return new_room
    return None


def update_room_description(location_id: str, room_id: str,
                            new_description: str,
                            image_prompt_day: str = None,
                            image_prompt_night: str = None) -> bool:
    """Aktualisiert Beschreibung und/oder Image-Prompts eines Raums. Returns True bei Erfolg."""
    # Validierung
    new_description = _validate_room_description(new_description)
    if not new_description and image_prompt_day is None and image_prompt_night is None:
        logger.warning("Raum-Beschreibung nach Validierung leer und kein image_prompt — Update abgelehnt")
        return False
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            for room in loc.get("rooms", []):
                if room.get("id") == room_id:
                    if new_description:
                        room["description"] = new_description
                    if image_prompt_day is not None:
                        if image_prompt_day != room.get("image_prompt_day", ""):
                            room["prompt_changed"] = True
                        room["image_prompt_day"] = image_prompt_day
                    if image_prompt_night is not None:
                        if image_prompt_night != room.get("image_prompt_night", ""):
                            room["prompt_changed"] = True
                        room["image_prompt_night"] = image_prompt_night
                    _save_world_data(data)
                    return True
    return False


def clear_room_prompt_changed(location_id: str, room_id: str) -> bool:
    """Entfernt das prompt_changed Flag von einem Raum. Returns True bei Erfolg."""
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            for room in loc.get("rooms", []):
                if room.get("id") == room_id:
                    if room.pop("prompt_changed", None):
                        _save_world_data(data)
                    return True
    return False


def clear_location_prompt_changed(location_id: str) -> bool:
    """Entfernt das prompt_changed Flag von einer Location. Returns True bei Erfolg."""
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            if loc.pop("prompt_changed", None):
                _save_world_data(data)
            return True
    return False


def track_room_activity(location_id: str,
    room_id: str,
    activity_name: str,
    auto_add_threshold: int = 3) -> bool:
    """Zaehlt Nutzung einer Aktivitaet im Raum hoch. Fuegt neue automatisch hinzu.

    Wird aufgerufen wenn ein Character eine (kurze) Aktivitaet ausfuehrt.
    Raum-Aktivitaeten sind ID-Referenzen auf die Bibliothek.
    - Existierende Referenz: nichts tun (bereits zugewiesen)
    - Neue Aktivitaet: als Kandidat tracken, ab Schwellwert als Bibliotheks-Referenz hinzufuegen

    Args:
        activity_name: Kurzer Kategorie-Name (max ~25 Zeichen, nie detail-Text)
        auto_add_threshold: Ab wie vielen Nutzungen eine neue Aktivitaet
            dauerhaft zum Raum hinzugefuegt wird.

    Returns:
        True wenn eine neue Aktivitaet zum Raum hinzugefuegt wurde.
    """
    if not activity_name or not location_id or not room_id:
        return False
    if len(activity_name) > 30:
        return False
    # Noise-Werte ignorieren — sonst landen Platzhalter als echte Aktivitaeten
    # in der Bibliothek und werden vom Scheduler zufaellig gewaehlt.
    if activity_name.strip().lower() in {"none", "null", "n/a", "-", "—", "undefined"}:
        return False

    # Bibliotheks-Aktivitaet per Name suchen
    from app.models.activity_library import find_library_activity_by_name
    lib_act = find_library_activity_by_name(activity_name)

    data = _load_world_data()
    added = False

    for loc in data.get("locations", []):
        if loc.get("id") != location_id:
            continue
        for room in loc.get("rooms", []):
            if room.get("id") != room_id:
                continue

            activities = room.get("activities", [])

            # Pruefen ob Aktivitaet bereits als Referenz zugewiesen
            if lib_act:
                act_id = lib_act.get("id", "")
                if act_id and act_id in activities:
                    # Bereits zugewiesen — nichts tun
                    return False

            # Auch per Name pruefen (fuer String-Referenzen ohne Bibliothek)
            act_lower = activity_name.lower().replace(" ", "_")
            if act_lower in activities:
                return False

            # Kandidaten-Tracking: _pending_activities am Raum
            pending = room.setdefault("_pending_activities", {})
            count = pending.get(activity_name, 0) + 1
            pending[activity_name] = count

            if count >= auto_add_threshold:
                if lib_act:
                    # Bibliotheks-Referenz hinzufuegen
                    act_id = lib_act.get("id", "")
                    if act_id:
                        activities.append(act_id)
                        room["activities"] = activities
                        added = True
                        logger.info("Auto-added library ref '%s' to room '%s' (used %dx)",
                                    act_id, room.get("name", room_id), count)
                else:
                    # Nicht in Bibliothek — zuerst in Bibliothek anlegen, dann referenzieren
                    from app.models.activity_library import save_library_activity
                    import re
                    new_id = re.sub(r'[^a-z0-9]+', '_', activity_name.lower()).strip('_')
                    save_library_activity({
                        "id": new_id,
                        "name": activity_name,
                        "description": "",
                        "_group": "Auto-erkannt",
                    }, target_dir="world")
                    activities.append(new_id)
                    room["activities"] = activities
                    added = True
                    logger.info("Auto-created library activity '%s' and added to room '%s' (used %dx)",
                                new_id, room.get("name", room_id), count)

                del pending[activity_name]
                if not pending:
                    del room["_pending_activities"]

            _save_world_data(data)
            return added

    return False


def _resolve_clones(locations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merged passable Klone mit ihrem Template.

    Klone speichern minimal: id, template_location_id, grid_x, grid_y und
    optional name. Beim Lesen werden alle uebrigen Felder vom Template
    geerbt, sodass Aenderungen am Template automatisch fuer alle Klone gelten.
    Behaelt template_location_id im Output, damit das Frontend sie aus dem
    Welt-Tree filtern kann.
    """
    by_id = {l.get("id"): l for l in locations if l.get("id")}
    resolved: List[Dict[str, Any]] = []
    for loc in locations:
        tmpl_id = (loc.get("template_location_id") or "").strip()
        if not tmpl_id:
            resolved.append(loc)
            continue
        tmpl = by_id.get(tmpl_id)
        if not tmpl:
            # Template geloescht — Klon wird zur Waise; spaeter beim
            # naechsten Save aufraeumen lassen, jetzt aber rendern.
            resolved.append(loc)
            continue
        merged = {**tmpl, **{
            k: v for k, v in loc.items()
            if k in ("id", "grid_x", "grid_y", "template_location_id")
            or v not in (None, "", [], {})
        }}
        # Template-Identitaet vergessen, sonst nimmt der Klon die ID des
        # Templates an. Override mit dem ECHTEN Klon-Identifier:
        merged["id"] = loc.get("id")
        merged["template_location_id"] = tmpl_id
        merged["grid_x"] = loc.get("grid_x")
        merged["grid_y"] = loc.get("grid_y")
        resolved.append(merged)
    return resolved


def list_locations() -> List[Dict[str, Any]]:
    """Gibt alle Orte eines Users zurueck (Klone gemerged mit Template)."""
    raw = _load_world_data().get("locations", [])
    return _resolve_clones(raw)


def resolve_location(identifier: str) -> Optional[Dict[str, Any]]:
    """Findet einen Ort per ID, Name oder Teilstring (Backwards-Compatibility).

    Sucht: 1) exakte ID, 2) exakter Name, 3) Teilstring-Match (bidirektional).
    """
    if not identifier:
        return None
    locations = list_locations()
    # 1) Exakte ID
    for location in locations:
        if location.get("id") == identifier:
            return location
    # 2) Exakter Name
    for location in locations:
        if location.get("name") == identifier:
            return location
    # 3) Teilstring: "Studentenwohnheim - Gemeinschaftsraum" matched "Studentenwohnheim"
    id_lower = identifier.lower()
    for location in locations:
        loc_name = location.get("name", "").lower()
        if loc_name and (loc_name in id_lower or id_lower in loc_name):
            return location
    return None


def get_location(identifier: str) -> Optional[Dict[str, Any]]:
    """Gibt einen Ort anhand von ID oder Name zurueck (Backwards-Compatible)."""
    return resolve_location(identifier)


# ============================================================
# KNOWLEDGE-ITEM VISIBILITY
# Ein Ort oder Raum kann ein Item verlangen, das der Character besitzen
# muss um diesen Ort/Raum zu "kennen" (im Picker/Chat/Scheduler sichtbar).
# Vererbung: ist das Item auf Location-Ebene gesetzt, gilt es automatisch
# auch fuer alle Raeume darunter — der Character muss es dann erst haben,
# bevor er ueberhaupt die Location sieht.
# ============================================================

def _character_has_item(character_name: str, item_id: str) -> bool:
    """Prueft ob der Character das angegebene Item im Inventar hat."""
    if not item_id or not character_name:
        return False
    try:
        from app.models.inventory import _load_inventory
        inv = _load_inventory(character_name).get("inventory", []) or []
    except Exception:
        return False
    for entry in inv:
        if entry.get("item_id") == item_id:
            return True
    return False


def _character_known_locations(character_name: str):
    """Liefert die explizit gesetzte known_locations-Liste oder None.

    None bedeutet: Feld nicht gesetzt → Legacy-Verhalten (keine Restriktion).
    Eine Liste (auch leer) bedeutet: strict — nur enthaltene Location-IDs sind
    sichtbar. Wird ueber Drag&Drop im Worldmap-Panel populiert.
    """
    if not character_name:
        return None
    try:
        from app.models.character import get_character_config
        cfg = get_character_config(character_name) or {}
    except Exception:
        return None
    if "known_locations" not in cfg:
        return None
    val = cfg.get("known_locations")
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return None


def location_visible_to_character(character_name: str,
                                    location: Dict[str, Any]) -> bool:
    """True wenn der Character das Wissens-Item der Location besitzt
    (oder keins gesetzt ist) UND die Location in seiner known_locations-Liste
    steht (falls die Liste explizit gesetzt ist).

    known_locations: optionale Liste von Location-IDs. Wenn das Feld fehlt
    (Legacy), gilt keine Listen-Restriktion. Wenn es gesetzt ist (auch leer),
    gilt strict membership — die Liste wird ueber Drag&Drop populiert.
    """
    if not isinstance(location, dict):
        return False
    iid = (location.get("knowledge_item_id") or "").strip()
    if iid and not _character_has_item(character_name, iid):
        return False
    known = _character_known_locations(character_name)
    if known is not None:
        loc_id = location.get("id") or ""
        if loc_id not in known:
            return False
    return True


def room_visible_to_character(character_name: str,
                                location: Dict[str, Any],
                                room: Dict[str, Any]) -> bool:
    """True wenn der Character sowohl das Location- als auch das Raum-
    Wissens-Item hat (beide optional)."""
    if not location_visible_to_character(character_name, location):
        return False
    if not isinstance(room, dict):
        return False
    iid = (room.get("knowledge_item_id") or "").strip()
    if not iid:
        return True
    return _character_has_item(character_name, iid)


def list_locations_for_character(character_name: str) -> List[Dict[str, Any]]:
    """Liefert alle Locations die der Character dank Wissens-Items sehen darf.
    Raeume werden pro Location ebenfalls gefiltert — nur sichtbare bleiben im
    zurueckgelieferten 'rooms'-Array.
    """
    visible = []
    for loc in list_locations():
        if not location_visible_to_character(character_name, loc):
            continue
        rooms = [r for r in (loc.get("rooms") or [])
                 if room_visible_to_character(character_name, loc, r)]
        visible.append({**loc, "rooms": rooms})
    return visible


def get_location_by_id(location_id: str) -> Optional[Dict[str, Any]]:
    """Gibt einen Ort per exakter ID-Suche zurueck."""
    if not location_id:
        return None
    for location in list_locations():
        if location.get("id") == location_id:
            return location
    return None


def get_location_name(location_id: str) -> str:
    """Gibt den Namen eines Ortes anhand seiner ID zurueck.

    Wenn die ID aufgeloest werden kann: Name zurueck.
    Wenn es wie eine Hex-ID aussieht aber nicht gefunden wird: "" (stale Referenz).
    Sonst (temporaerer Ortsname wie "Café"): Wert direkt zurueck.
    """
    loc = resolve_location(location_id)
    if loc:
        return loc.get("name", location_id)
    # Hex-ID die nicht aufgeloest werden konnte = geloeschter Ort
    if re.match(r'^[0-9a-f]{8}$', location_id):
        return ""
    # Temporaerer Ortsname (z.B. "Café") — direkt zurueckgeben
    return location_id


def get_location_id(identifier: str) -> str:
    """Gibt die ID eines Ortes zurueck (per ID oder Name gesucht).

    Nuetzlich um von Name auf ID zu konvertieren.
    """
    loc = resolve_location(identifier)
    if loc:
        return loc.get("id", "")
    return ""


def add_location(name: str, description: str,
                  rooms: List[Dict[str, Any]] = None,
                  activities: List[Dict[str, str]] = None,
                  image_prompt_day: str = None,
                  image_prompt_night: str = None,
                  image_prompt_map: str = None) -> Dict[str, Any]:
    """Fuegt einen neuen Ort hinzu oder aktualisiert einen bestehenden.

    Args:
        rooms: Liste von {id, name, description, activities} Objekten
        activities: Legacy — wird ignoriert wenn rooms angegeben
        image_prompt_day: Prompt fuer Hintergrundbild bei Tag (6-18 Uhr)
        image_prompt_night: Prompt fuer Hintergrundbild bei Nacht (18-6 Uhr)
        image_prompt_map: Prompt fuer Kartenbild
    """
    data = _load_world_data()
    locations = data.get("locations", [])

    # Room-IDs sicherstellen
    if rooms is not None:
        for room in rooms:
            if not room.get("id"):
                room["id"] = _generate_room_id()

    # Suche per Name (Update)
    for location in locations:
        if location.get("name") == name:
            location["description"] = description
            if rooms is not None:
                # Alte Rooms als Lookup fuer prompt_changed-Vergleich UND
                # Server-State-Erhalt (items, prompt_changed, etc.). Die FE
                # schickt beim Raum-Edit nur die Felder die sie kennt — Items,
                # die separat ueber /inventory/rooms platziert wurden, fehlen
                # in der FE-Liste und wuerden sonst beim Save geloescht.
                old_rooms_by_id = {r["id"]: r for r in location.get("rooms", []) if r.get("id")}
                # Felder die NICHT vom Raum-Editor verwaltet werden — bei
                # Update aus dem Bestand uebernehmen wenn nicht mitgegeben.
                _server_state_fields = ("items",)
                for room in rooms:
                    old_room = old_rooms_by_id.get(room.get("id"))
                    if old_room:
                        # Server-State-Felder erhalten falls FE sie weggelassen hat
                        for fld in _server_state_fields:
                            if fld not in room and fld in old_room:
                                room[fld] = old_room[fld]
                        # Nur prompt_changed setzen wenn sich Prompts tatsaechlich geaendert haben
                        day_changed = room.get("image_prompt_day", "") != old_room.get("image_prompt_day", "")
                        night_changed = room.get("image_prompt_night", "") != old_room.get("image_prompt_night", "")
                        if day_changed or night_changed:
                            room["prompt_changed"] = True
                        else:
                            # Bestehenden prompt_changed-Status beibehalten
                            if old_room.get("prompt_changed"):
                                room["prompt_changed"] = True
                    else:
                        # Neuer Raum — Flag setzen wenn Prompts vorhanden
                        if room.get("image_prompt_day") or room.get("image_prompt_night"):
                            room.setdefault("prompt_changed", True)
                location["rooms"] = rooms
                location.pop("activities", None)
            if image_prompt_day is not None:
                if image_prompt_day != location.get("image_prompt_day", ""):
                    location["prompt_changed"] = True
                location["image_prompt_day"] = image_prompt_day
            if image_prompt_night is not None:
                if image_prompt_night != location.get("image_prompt_night", ""):
                    location["prompt_changed"] = True
                location["image_prompt_night"] = image_prompt_night
            if image_prompt_map is not None:
                location["image_prompt_map"] = image_prompt_map
            # ID nachrüsten falls fehlend
            if not location.get("id"):
                location["id"] = _generate_location_id()
            _save_world_data(data)
            return location

    # Neue Location — prompt_changed fuer alle Rooms mit Prompts setzen
    if rooms is not None:
        for room in rooms:
            if room.get("image_prompt_day") or room.get("image_prompt_night"):
                room.setdefault("prompt_changed", True)
    new_location = {
        "id": _generate_location_id(),
        "name": name,
        "description": description,
        "rooms": rooms or [],
        "image_prompt_day": image_prompt_day or "",
        "image_prompt_night": image_prompt_night or "",
        "image_prompt_map": image_prompt_map or "",
    }
    if image_prompt_day or image_prompt_night:
        new_location["prompt_changed"] = True
    locations.append(new_location)
    data["locations"] = locations
    _save_world_data(data)
    return new_location


def rename_location(location_id: str, new_name: str) -> Optional[Dict[str, Any]]:
    """Benennt einen Ort um. ID bleibt gleich."""
    data = _load_world_data()
    locations = data.get("locations", [])

    for location in locations:
        if location.get("id") == location_id:
            location["name"] = new_name
            _save_world_data(data)
            return location
    return None


def get_neighbor_location_ids(location_id: str) -> List[str]:
    """Gibt die IDs der benachbarten Locations zurueck (8 Felder um das eigene auf der Karte)."""
    data = _load_world_data()
    locations = data.get("locations", [])

    # Position des Quell-Orts finden
    source = None
    for loc in locations:
        if loc.get("id") == location_id and loc.get("grid_x") is not None:
            source = loc
            break
    if not source:
        return []

    sx, sy = source["grid_x"], source["grid_y"]
    neighbors = []
    for loc in locations:
        if loc.get("id") == location_id:
            continue
        gx, gy = loc.get("grid_x"), loc.get("grid_y")
        if gx is not None and gy is not None:
            if abs(gx - sx) <= 1 and abs(gy - sy) <= 1:
                neighbors.append(loc["id"])
    return neighbors


def find_path_through_known(start_id: str, target_id: str,
                             known_ids: List[str]) -> Optional[List[str]]:
    """BFS ueber das Grid, traversiert nur ``known_ids``-Locations.

    Returns die Pfad-Liste (inkl. Start und Target) oder None wenn nicht
    erreichbar. Start muss nicht in known_ids sein (Character steht dort);
    Alle Zwischenstationen muessen in known_ids sein.

    Direkte Grid-Nachbarn des Starts duerfen auch dann betreten werden, wenn
    sie nicht in known_ids stehen — ein Character sieht das Nachbardorf vom
    eigenen Standort aus und kann es ohne formelle "Discovery" erreichen
    (Discovery wird beim Ankommen automatisch nachgezogen).
    """
    if not start_id or not target_id:
        return None
    if start_id == target_id:
        return [start_id]
    walkable = set(known_ids or [])
    walkable.add(start_id)
    # Direkte Grid-Nachbarn vom Start dazunehmen — egal ob known oder nicht.
    # So bleiben Multi-Hop-Strecken durch Unbekanntes blockiert, aber der
    # naechste Ort um die Ecke ist immer erreichbar.
    direct_neighbors = set(get_neighbor_location_ids(start_id))
    walkable.update(direct_neighbors)
    if target_id not in walkable:
        return None

    from collections import deque
    queue = deque([start_id])
    parents: Dict[str, str] = {start_id: ""}
    while queue:
        node = queue.popleft()
        if node == target_id:
            # Pfad rekonstruieren
            path = [node]
            while parents.get(path[-1]):
                path.append(parents[path[-1]])
            path.reverse()
            return path
        for nb in get_neighbor_location_ids(node):
            if nb in parents or nb not in walkable:
                continue
            parents[nb] = node
            queue.append(nb)
    return None


def next_step_toward(character_name: str, target_id: str) -> Optional[str]:
    """Liefert die Location-ID des naechsten Schritts vom aktuellen Standort
    Richtung Ziel. Pfad nur ueber known_locations.

    Returns:
        - None wenn kein Pfad existiert oder Character bereits am Ziel
        - sonst die Nachbar-Location-ID, auf die als Naechstes gewechselt
          werden soll
    """
    from app.models.character import (
        get_character_current_location, get_known_locations)
    current = get_character_current_location(character_name) or ""
    if not current or current == target_id:
        return None
    known = get_known_locations(character_name) or []
    path = find_path_through_known(current, target_id, known)
    if not path or len(path) < 2:
        return None
    return path[1]


def update_location_position(location_id: str, grid_x: int, grid_y: int) -> Optional[Dict[str, Any]]:
    """Setzt die Raster-Position eines Ortes. grid_x/grid_y < 0 entfernt die Position."""
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            if grid_x < 0 or grid_y < 0:
                loc.pop("grid_x", None)
                loc.pop("grid_y", None)
            else:
                loc["grid_x"] = grid_x
                loc["grid_y"] = grid_y
            _save_world_data(data)
            return loc
    return None


def cleanup_orphan_clones() -> Dict[str, int]:
    """Bereinigt Klon-Datensaetze:

    - Klone ohne Grid-Position (off-map) -> loeschen.
    - Klone mit nicht-existierendem template_location_id -> loeschen.
    - Mehrere Klone an derselben Grid-Zelle (gleiches Template) -> nur den
      ersten behalten, Rest loeschen.

    Idempotent. Returns Stats-Dict.
    """
    data = _load_world_data()
    locations = data.get("locations", [])
    existing_ids = {l.get("id") for l in locations if l.get("id")}

    delete_ids: set = set()
    seen_cells: set = set()  # (template_id, grid_x, grid_y)

    # Erste Schleife: Off-Map und Waisen markieren
    for loc in locations:
        tid = (loc.get("template_location_id") or "").strip()
        if not tid:
            continue
        # Waise: Template existiert nicht mehr
        if tid not in existing_ids:
            delete_ids.add(loc.get("id"))
            continue
        gx = loc.get("grid_x")
        gy = loc.get("grid_y")
        # Off-Map: kein Grid oder negativ
        if gx is None or gy is None or gx < 0 or gy < 0:
            delete_ids.add(loc.get("id"))
            continue

    # Zweite Schleife: Duplikate pro (Template, Grid-Zelle)
    for loc in locations:
        tid = (loc.get("template_location_id") or "").strip()
        if not tid or loc.get("id") in delete_ids:
            continue
        gx = loc.get("grid_x")
        gy = loc.get("grid_y")
        cell = (tid, gx, gy)
        if cell in seen_cells:
            delete_ids.add(loc.get("id"))
        else:
            seen_cells.add(cell)

    if not delete_ids:
        return {"removed": 0, "off_map": 0, "duplicates": 0,
                "orphan_template": 0, "kept": len(locations)}

    new_locations = [l for l in locations if l.get("id") not in delete_ids]
    data["locations"] = new_locations
    _save_world_data(data)

    # Stats unterscheiden
    off_map = duplicates = orphan = 0
    for loc in locations:
        if loc.get("id") not in delete_ids:
            continue
        tid = (loc.get("template_location_id") or "").strip()
        gx, gy = loc.get("grid_x"), loc.get("grid_y")
        if tid not in existing_ids:
            orphan += 1
        elif gx is None or gy is None or gx < 0 or gy < 0:
            off_map += 1
        else:
            duplicates += 1

    logger.info("cleanup_orphan_clones: removed=%d (off_map=%d, duplicates=%d, orphan=%d)",
                len(delete_ids), off_map, duplicates, orphan)
    return {"removed": len(delete_ids),
            "off_map": off_map,
            "duplicates": duplicates,
            "orphan_template": orphan,
            "kept": len(new_locations)}


def clone_location(template_id: str, grid_x: int, grid_y: int) -> Optional[Dict[str, Any]]:
    """Erzeugt eine neue Klon-Instanz von einem (passable) Template.

    Klon speichert minimal: id, template_location_id, grid_x, grid_y. Alle
    sonstigen Felder werden zur Lesezeit aus dem Template gemerged.
    Returns das resolved-Dict des Klons oder None bei Fehler.
    """
    if not template_id:
        return None
    # Garde: Klone ohne gueltige Grid-Position landen nicht in der DB.
    try:
        gx = int(grid_x)
        gy = int(grid_y)
    except (TypeError, ValueError):
        return None
    if gx < 0 or gy < 0:
        return None
    data = _load_world_data()
    template = None
    for loc in data.get("locations", []):
        if loc.get("id") == template_id:
            template = loc
            break
    if not template:
        return None
    # Doppelte Klone derselben Template-Zelle vermeiden — der erste Klon
    # gewinnt, weitere Drops auf dieselbe Zelle werden verworfen.
    for loc in data.get("locations", []):
        if (loc.get("template_location_id") or "") == template_id \
                and loc.get("grid_x") == gx and loc.get("grid_y") == gy:
            logger.info("clone_location: existierender Klon an (%d,%d) fuer "
                        "Template %s, kein neuer Eintrag", gx, gy, template_id)
            return loc
    new_id = _generate_location_id()
    clone = {
        "id": new_id,
        "template_location_id": template_id,
        "grid_x": gx,
        "grid_y": gy,
        "rooms": [],
    }
    data["locations"].append(clone)
    _save_world_data(data)
    # Resolved zurueckgeben — Frontend bekommt die merge-fertige Instanz.
    for loc in _resolve_clones(data["locations"]):
        if loc.get("id") == new_id:
            return loc
    return clone


def delete_location(identifier: str) -> bool:
    """Loescht einen Ort per ID oder Name. Wenn ein Template geloescht wird,
    werden alle Klone (Locations mit template_location_id == template_id)
    kaskadierend mitentfernt.
    """
    data = _load_world_data()
    locations = data.get("locations", [])
    # Ziel-IDs ermitteln: das Original und ggf. abhaengige Klone
    target_ids = set()
    for loc in locations:
        if loc.get("id") == identifier or loc.get("name") == identifier:
            target_ids.add(loc.get("id"))
    if not target_ids:
        return False
    # Cascade: alle Klone deren template in target_ids ist
    cascade = True
    while cascade:
        cascade = False
        for loc in locations:
            tid = (loc.get("template_location_id") or "").strip()
            lid = loc.get("id")
            if tid and tid in target_ids and lid and lid not in target_ids:
                target_ids.add(lid)
                cascade = True

    new_locations = [loc for loc in locations if loc.get("id") not in target_ids]
    if len(new_locations) < len(locations):
        data["locations"] = new_locations
        _save_world_data(data)
        return True
    return False


# === Aktivitaeten (eingebettet in Orte) ===

def get_activity(activity_name: str) -> Optional[Dict[str, str]]:
    """Sucht eine Aktivitaet ueber alle Orte und Raeume hinweg."""
    for location in list_locations():
        for room in location.get("rooms", []):
            for act in room.get("activities", []):
                if isinstance(act, dict) and act.get("name") == activity_name:
                    return act
                elif isinstance(act, str) and act == activity_name:
                    return {"name": act, "description": ""}
    return None


# === Hintergrundbilder ===

def get_background_path(location_identifier: str, room: str = "",
                        hour: int = -1, strict_room: bool = False) -> Optional[Path]:
    """Gibt den Pfad zu einem zufaellig gewaehlten Hintergrundbild zurueck.

    Regeln:
    - Raum gesetzt UND Raum hat Bilder → eines der Raum-Bilder (Tag/Nacht bevorzugt)
    - Raum nicht gesetzt ODER Raum hat keine Bilder → eines der Location-Bilder
      (nicht raum-zugeordnet, Tag/Nacht bevorzugt) — ausser ``strict_room=True``
    - Location nicht gesetzt ODER Location hat keine Bilder → None

    Args:
        hour: Aktuelle Stunde (0-23). -1 = keine Tageszeit-Filterung.
        strict_room: Wenn True und ``room`` gesetzt: KEIN Fallback auf
            Location-Default. Liefert None wenn der Raum keine dedizierten
            Bilder hat. Verwendet vom Regenerate-Pfad, damit ein expliziter
            Raumwechsel im Dialog nicht stillschweigend dieselbe Default-
            Datei zurueckgibt (User wuerde den Wechsel nie bemerken).
    """
    loc = resolve_location(location_identifier)
    if not loc:
        return None
    loc_id = loc.get("id", "")
    if not loc_id:
        return None

    # Neue Liste oder Fallback auf altes Einzelfeld
    bg_images = loc.get("background_images", [])
    if not bg_images and loc.get("background_image"):
        bg_images = [loc["background_image"]]
    if not bg_images:
        return None

    # Klone teilen das Bildmaterial des Templates — Lookups laufen ueber
    # die Owner-ID (Template-ID bei Klonen, sonst eigene ID).
    owner_id = _gallery_owner_id(location_identifier) or loc_id
    gallery_base = get_storage_dir() / "world_gallery" / owner_id

    # Nur existierende Bilder beruecksichtigen
    valid = [img for img in bg_images if (gallery_base / img).exists()]
    if not valid:
        return None

    image_rooms = get_gallery_image_rooms(owner_id)
    image_types = get_gallery_image_types(owner_id)

    def _not_map(img: str) -> bool:
        return image_types.get(img, "") != "map"

    # Kandidaten-Auswahl nach Regel:
    # 1) Raum gesetzt → Raum-Bilder versuchen
    # 2) Wenn keine Raum-Bilder / kein Raum → Location-Bilder (ohne Raum-Tag)
    candidates: List[str] = []
    if room:
        candidates = [img for img in valid if image_rooms.get(img, "") == room and _not_map(img)]
        if not candidates and strict_room:
            # Strikter Modus: User hat den Raum bewusst gewaehlt — KEIN Fallback.
            return None
    if not candidates:
        candidates = [img for img in valid if image_rooms.get(img, "") == "" and _not_map(img)]
    if not candidates:
        return None

    # Tageszeit bestimmen
    time_type = ""
    if 0 <= hour <= 23:
        time_type = "day" if 6 <= hour < 18 else "night"

    # Tag/Nacht bevorzugen
    if time_type:
        timed = [img for img in candidates if image_types.get(img, "") == time_type]
        if timed:
            return gallery_base / _random.choice(timed)

    # Bilder ohne Tageszeit-Zuordnung (neutral) bevorzugen gegenueber dem
    # jeweils unpassenden Typ.
    untyped = [img for img in candidates if not image_types.get(img, "")]
    if untyped:
        return gallery_base / _random.choice(untyped)

    return gallery_base / _random.choice(candidates)


def get_background_images(location_id: str) -> List[str]:
    """Gibt die Liste der als Hintergrund markierten Bilder zurueck."""
    loc = get_location_by_id(location_id)
    if not loc:
        return []
    bg_images = loc.get("background_images", [])
    if not bg_images and loc.get("background_image"):
        bg_images = [loc["background_image"]]
    return bg_images


def toggle_background_image(location_id: str, image_name: str) -> bool:
    """Toggled ob ein Bild als Hintergrund markiert ist.

    Returns True wenn das Bild jetzt markiert ist, False wenn entfernt.
    """
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            bg_images = loc.get("background_images", [])
            # Altes Einzelfeld migrieren
            if "background_image" in loc:
                old_bg = loc.pop("background_image", "")
                if old_bg and old_bg not in bg_images:
                    bg_images.append(old_bg)

            if image_name in bg_images:
                bg_images.remove(image_name)
                loc["background_images"] = bg_images
                _save_world_data(data)
                return False
            else:
                bg_images.append(image_name)
                loc["background_images"] = bg_images
                _save_world_data(data)
                return True
    return False


def remove_background_image(location_id: str, image_name: str) -> None:
    """Entfernt ein Bild aus der Hintergrund-Liste (z.B. bei Bild-Loeschung)."""
    data = _load_world_data()
    for loc in data.get("locations", []):
        if loc.get("id") == location_id:
            bg_images = loc.get("background_images", [])
            if image_name in bg_images:
                bg_images.remove(image_name)
                loc["background_images"] = bg_images
                _save_world_data(data)


def _gallery_owner_id(location_identifier: str) -> str:
    """Liefert die ID, unter der die Galerie-Bilder eines Ortes liegen.

    Fuer Klone (template_location_id gesetzt) gibt sie die Template-ID
    zurueck — Klone teilen sich das Bildmaterial mit ihrem Template.
    Fuer eigenstaendige Locations die eigene ID. Wird von Galerie- und
    Hintergrund-Lookups genutzt.
    """
    loc = resolve_location(location_identifier)
    if not loc:
        return ""
    tmpl_id = (loc.get("template_location_id") or "").strip()
    if tmpl_id:
        return tmpl_id
    return loc.get("id", "") or ""


def get_gallery_dir(location_identifier: str) -> Path:
    """Gibt den Pfad zum Galerie-Verzeichnis eines Ortes zurueck.

    Akzeptiert ID oder Name. Verwendet die Location-ID fuer den Dateipfad
    — Klone werden auf ihre Template-ID umgeleitet, damit alle Klone das
    gleiche Bildmaterial sehen.
    """
    owner_id = _gallery_owner_id(location_identifier)
    if owner_id:
        dir_name = owner_id
    else:
        dir_name = re.sub(r'[^\w\-]', '_', location_identifier)
    return get_storage_dir() / "world_gallery" / dir_name


def list_gallery_images(location_name: str) -> List[str]:
    """Gibt alle Galerie-Bilder eines Ortes zurueck (Dateinamen, neueste zuerst)."""
    gallery_dir = get_gallery_dir(location_name)
    if not gallery_dir.exists():
        return []
    images = sorted(
        [f.name for f in gallery_dir.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')],
        reverse=True
    )
    return images


def save_gallery_prompt(location_name: str, image_name: str, prompt: str):
    """Speichert den Generierungs-Prompt zu einem Galerie-Bild."""
    gallery_dir = get_gallery_dir(location_name)
    prompts_file = gallery_dir / "prompts.json"
    prompts = {}
    if prompts_file.exists():
        try:
            prompts = json.loads(prompts_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    prompts[image_name] = prompt
    gallery_dir.mkdir(parents=True, exist_ok=True)
    prompts_file.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")


def get_all_gallery_prompts(location_name: str) -> Dict[str, str]:
    """Gibt alle gespeicherten Prompts eines Ortes zurueck."""
    gallery_dir = get_gallery_dir(location_name)
    prompts_file = gallery_dir / "prompts.json"
    if prompts_file.exists():
        try:
            return json.loads(prompts_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_gallery_meta(location_name: str) -> dict:
    """Laedt gallery_meta.json (upgraded-Status etc.)."""
    gallery_dir = get_gallery_dir(location_name)
    meta_file = gallery_dir / "gallery_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_gallery_meta(location_name: str, meta: dict):
    """Speichert gallery_meta.json."""
    gallery_dir = get_gallery_dir(location_name)
    gallery_dir.mkdir(parents=True, exist_ok=True)
    meta_file = gallery_dir / "gallery_meta.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")



def set_gallery_image_room(location_name: str, image_name: str, room_id: str):
    """Setzt den Raum eines Galerie-Bildes."""
    meta = _load_gallery_meta(location_name)
    rooms = meta.get("rooms", {})
    if room_id:
        rooms[image_name] = room_id
    else:
        rooms.pop(image_name, None)
    meta["rooms"] = rooms
    _save_gallery_meta(location_name, meta)


def find_room_by_gallery_image(image_name: str) -> tuple:
    """Sucht einen Raum/Ort anhand eines Galerie-Bildnamens.

    Iteriert ueber alle world_gallery-Ordner und prueft ob die Datei dort liegt
    und ggf. einem Raum zugeordnet ist.

    Returns: (location_id, room_id) — beides leer wenn nicht gefunden.
    """
    base = get_storage_dir() / "world_gallery"
    if not base.exists() or not image_name:
        return ("", "")
    for loc_dir in base.iterdir():
        if not loc_dir.is_dir():
            continue
        if not (loc_dir / image_name).exists():
            continue
        # Bild gefunden — Raum-Zuordnung aus gallery_meta.json lesen
        meta_file = loc_dir / "gallery_meta.json"
        room_id = ""
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                room_id = (meta.get("rooms") or {}).get(image_name, "")
            except Exception:
                pass
        return (loc_dir.name, room_id)
    return ("", "")


def get_gallery_image_rooms(location_name: str) -> Dict[str, str]:
    """Gibt alle Bild-Raum-Zuordnungen zurueck."""
    meta = _load_gallery_meta(location_name)
    return meta.get("rooms", {})


def remove_gallery_image_room(location_name: str, image_name: str):
    """Entfernt die Raum-Zuordnung eines geloeschten Bildes."""
    meta = _load_gallery_meta(location_name)
    rooms = meta.get("rooms", {})
    if image_name in rooms:
        del rooms[image_name]
        meta["rooms"] = rooms
        _save_gallery_meta(location_name, meta)


# === Bild-Typ-Zuordnung (day/night/map) ===

def set_gallery_image_type(location_name: str, image_name: str, image_type: str):
    """Setzt den Typ eines Galerie-Bildes: 'day', 'night', 'map' oder '' (kein Typ)."""
    meta = _load_gallery_meta(location_name)
    types = meta.get("image_types", {})
    if image_type:
        types[image_name] = image_type
    else:
        types.pop(image_name, None)
    meta["image_types"] = types
    _save_gallery_meta(location_name, meta)


def get_gallery_image_types(location_name: str) -> Dict[str, str]:
    """Gibt alle Bild-Typ-Zuordnungen zurueck: {image_name: 'day'|'night'|'map'}."""
    meta = _load_gallery_meta(location_name)
    return meta.get("image_types", {})


def remove_gallery_image_type(location_name: str, image_name: str):
    """Entfernt die Typ-Zuordnung eines geloeschten Bildes."""
    meta = _load_gallery_meta(location_name)
    types = meta.get("image_types", {})
    if image_name in types:
        del types[image_name]
        meta["image_types"] = types
        _save_gallery_meta(location_name, meta)


def set_gallery_image_meta(location_name: str, image_name: str, meta_info: dict):
    """Speichert Erzeugungs-Metadaten (Backend, Model etc.) fuer ein Galerie-Bild."""
    meta = _load_gallery_meta(location_name)
    image_metas = meta.get("image_metas", {})
    image_metas[image_name] = meta_info
    meta["image_metas"] = image_metas
    _save_gallery_meta(location_name, meta)


def get_gallery_image_metas(location_name: str) -> Dict[str, dict]:
    """Gibt alle Bild-Metadaten zurueck: {image_name: {backend: ..., model: ...}}."""
    meta = _load_gallery_meta(location_name)
    return meta.get("image_metas", {})


def list_all_activities() -> List[Dict[str, str]]:
    """Gibt eine flache, deduplizierte Liste aller Aktivitaeten zurueck."""
    seen = {}
    for location in list_locations():
        for room in location.get("rooms", []):
            for act in room.get("activities", []):
                if isinstance(act, dict):
                    name = act.get("name", "")
                    if name and name not in seen:
                        seen[name] = act
                elif isinstance(act, str) and act not in seen:
                    seen[act] = {"name": act, "description": ""}
    return list(seen.values())


# === Room-Migration ===


# === Location-ID Migration ===

def migrate_location_ids():
    """Fuegt persistente IDs zu bestehenden Locations hinzu und migriert Referenzen.

    Wird beim Server-Start aufgerufen. Idempotent: ueberspringt bereits migrierte User.

    Schritte:
    1. Fuer jeden User world.json laden
    2. Locations ohne 'id' bekommen eine neue ID
    3. Filesystem-Pfade umbenennen (backgrounds, gallery)
    4. Alle Referenzen in Character-Profilen, Configs, Scheduler etc. umschreiben
    """
    sd = get_storage_dir()
    if not sd.exists():
        return

    # Single-world: world.json lives directly in storage root
    user_dir = sd
    world_file = sd / "world.json"
    if not world_file.exists():
        return

    try:
        world_data = json.loads(world_file.read_text(encoding="utf-8"))
    except Exception:
        return

    locations = world_data.get("locations", [])
    if not locations:
        return

    changed = False

    # Phase 1: IDs zuweisen (falls noetig)
    name_to_id = {}
    needs_ids = any(not loc.get("id") for loc in locations)
    if needs_ids:
        for loc in locations:
            if not loc.get("id"):
                loc["id"] = _generate_location_id()
            name_to_id[loc["name"]] = loc["id"]
        changed = True

    # Phase 2: Filesystem bereinigen + backgrounds migrieren (IMMER)
    fs_changed = _migrate_filesystem_and_backgrounds(user_dir, locations)
    if fs_changed:
        changed = True

    # world.json speichern wenn geaendert
    if changed:
        world_data["locations"] = locations
        world_file.write_text(
            json.dumps(world_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # Referenzen migrieren (nur bei neuer ID-Zuweisung)
    if needs_ids:
        _migrate_references_to_ids(user_dir, name_to_id)
        logger.info("Location-IDs migriert")


def _migrate_filesystem_and_backgrounds(user_dir: Path, locations: List[Dict]) -> bool:
    """Migriert backgrounds/ nach gallery/ und setzt background_image Referenzen.

    Laeuft bei JEDEM Server-Start fuer ALLE User:
    - Gallery-Ordner: safe_name/ -> id/ umbenennen (falls noch noetig)
    - backgrounds/{id_or_name}.png -> gallery/{id}/ verschieben
    - background_image Referenz in Location setzen
    - Leeres backgrounds/ Verzeichnis aufraemen

    Returns True wenn world.json-Aenderungen vorgenommen wurden.
    """
    import shutil

    world_dir = user_dir / "world"
    if not world_dir.exists():
        return False

    bg_dir = world_dir / "backgrounds"
    gallery_dir = get_storage_dir() / "world_gallery"
    changed = False

    for loc in locations:
        loc_id = loc.get("id", "")
        if not loc_id:
            continue
        loc_name = loc.get("name", "")
        safe_name = re.sub(r'[^\w\-]', '_', loc_name)

        # Gallery: safe_name/ -> id/ umbenennen
        if gallery_dir.exists() and safe_name != loc_id:
            old_gallery = gallery_dir / safe_name
            new_gallery = gallery_dir / loc_id
            if old_gallery.exists() and not new_gallery.exists():
                old_gallery.rename(new_gallery)
                logger.info("Gallery umbenannt: %s/ -> %s/", safe_name, loc_id)

        # Altes Einzelfeld zu Liste migrieren
        if loc.get("background_image") and not loc.get("background_images"):
            loc["background_images"] = [loc.pop("background_image")]
            changed = True
        elif loc.get("background_image") and loc.get("background_images"):
            old_bg = loc.pop("background_image")
            if old_bg not in loc["background_images"]:
                loc["background_images"].append(old_bg)
            changed = True
        elif "background_image" in loc:
            loc.pop("background_image")
            changed = True

        # Ungueltige Eintraege in background_images bereinigen
        if loc.get("background_images"):
            loc_gallery = gallery_dir / loc_id if gallery_dir.exists() else None
            if loc_gallery:
                valid = [img for img in loc["background_images"] if (loc_gallery / img).exists()]
                if len(valid) != len(loc["background_images"]):
                    loc["background_images"] = valid
                    changed = True
            if loc["background_images"]:
                continue

        # backgrounds/{id}.png oder {safe_name}.png -> gallery/{id}/ verschieben
        if bg_dir.exists():
            bg_file = None
            for candidate in [bg_dir / f"{loc_id}.png", bg_dir / f"{safe_name}.png"]:
                if candidate.exists():
                    bg_file = candidate
                    break

            if bg_file:
                loc_gallery = gallery_dir / loc_id
                loc_gallery.mkdir(parents=True, exist_ok=True)
                ts = int(bg_file.stat().st_mtime)
                dest = loc_gallery / f"{ts}.png"
                if not dest.exists():
                    shutil.move(str(bg_file), str(dest))
                    logger.info("Background migriert: %s -> gallery/%s/%s", bg_file.name, loc_id, dest.name)
                else:
                    bg_file.unlink()
                    logger.info("Background entfernt (bereits in Gallery): %s", bg_file.name)
                bg_list = loc.get("background_images", [])
                if dest.name not in bg_list:
                    bg_list.append(dest.name)
                loc["background_images"] = bg_list
                changed = True

        # Keine background_images gesetzt -> neuestes Gallery-Bild nehmen
        if not loc.get("background_images"):
            loc_gallery = gallery_dir / loc_id if gallery_dir.exists() else None
            if loc_gallery and loc_gallery.exists():
                images = sorted(
                    [f.name for f in loc_gallery.iterdir()
                     if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')],
                    reverse=True
                )
                if images:
                    loc["background_images"] = [images[0]]
                    changed = True
                    logger.info("Background-Referenz gesetzt: %s -> %s", loc_name, images[0])

    # backgrounds/ Ordner aufraemen wenn leer
    if bg_dir.exists():
        remaining = [f for f in bg_dir.iterdir()]
        if not remaining:
            bg_dir.rmdir()
            logger.info("Leeres backgrounds/ Verzeichnis entfernt")

    return changed


def _migrate_references_to_ids(user_dir: Path, name_to_id: Dict[str, str]):
    """Migriert alle Location-Name-Referenzen zu IDs in Character-Daten.

    Migriert:
    - character_profile.json: current_location, outfits[].location
    - character_config.json: allowed_locations
    - scheduler/jobs.json: action.location
    - scheduler/daily_schedule.json: slots[].location
    - User-Profile: current_location
    """
    username = user_dir.name

    # Character-Verzeichnisse
    for subdir_name in ("characters", "agents"):
        chars_dir = user_dir / subdir_name
        if not chars_dir.exists():
            continue
        for char_dir in chars_dir.iterdir():
            if not char_dir.is_dir():
                continue
            _migrate_character_refs(char_dir, name_to_id)

    # User-Profile ({username}.json im Storage-Root)
    profile_path = get_storage_dir() / f"{username}.json"
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            changed = False
            cur_loc = profile.get("current_location", "")
            if cur_loc and cur_loc in name_to_id:
                profile["current_location"] = name_to_id[cur_loc]
                changed = True
            if changed:
                profile_path.write_text(
                    json.dumps(profile, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception:
            pass


def _migrate_character_refs(char_dir: Path, name_to_id: Dict[str, str]):
    """Migriert Location-Referenzen in einem Character-Verzeichnis."""
    # 1. character_profile.json
    profile_path = char_dir / "character_profile.json"
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            changed = False

            # current_location
            cur_loc = profile.get("current_location", "")
            if cur_loc and cur_loc in name_to_id:
                profile["current_location"] = name_to_id[cur_loc]
                changed = True

            # outfits[].location
            for outfit in profile.get("outfits", []):
                loc = outfit.get("location", "")
                if loc and loc in name_to_id:
                    outfit["location"] = name_to_id[loc]
                    changed = True

            if changed:
                profile_path.write_text(
                    json.dumps(profile, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception:
            pass

    # 2. character_config.json
    config_path = char_dir / "character_config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            changed = False

            # allowed_locations
            allowed = config.get("allowed_locations", [])
            if allowed:
                new_allowed = [name_to_id.get(loc, loc) for loc in allowed]
                if new_allowed != allowed:
                    config["allowed_locations"] = new_allowed
                    changed = True

            if changed:
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception:
            pass

    # 3. scheduler/jobs.json
    jobs_path = char_dir / "scheduler" / "jobs.json"
    if jobs_path.exists():
        try:
            jobs_data = json.loads(jobs_path.read_text(encoding="utf-8"))
            changed = False
            for job in jobs_data.get("jobs", []):
                action = job.get("action", {})
                loc = action.get("location", "")
                if loc and loc in name_to_id:
                    action["location"] = name_to_id[loc]
                    changed = True
            if changed:
                jobs_path.write_text(
                    json.dumps(jobs_data, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception:
            pass

    # 4. scheduler/daily_schedule.json
    schedule_path = char_dir / "scheduler" / "daily_schedule.json"
    if schedule_path.exists():
        try:
            schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
            changed = False
            for slot in schedule.get("slots", []):
                loc = slot.get("location", "")
                if loc and loc in name_to_id:
                    slot["location"] = name_to_id[loc]
                    changed = True
            if changed:
                schedule_path.write_text(
                    json.dumps(schedule, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception:
            pass

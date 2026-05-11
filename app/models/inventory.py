"""Inventory System - Gegenstaende, Raum-Items und Character-Inventar.

Drei Ebenen:
1. Item-Definitionen (user-level): Was existiert in der Welt → world.db:items
2. Raum-Items: Welche Items liegen wo (in locations/rooms) → world.db:rooms meta
3. Character-Inventar: Was traegt ein Character bei sich → world.db:inventory_items

Storage:
- Item-Definitionen: world.db:items (Welt) + shared/items/*.json (global)
- Item-Bilder: storage/items/{item_id}/image.png (bleibt auf Disk)
- Character-Inventar: world.db:inventory_items, equipped_pieces, equipped_items
- Raum-Items: Eingebettet in world.db:rooms meta (via world-Funktionen)
"""
import json
import random
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("inventory")

from app.core.paths import get_storage_dir

VALID_ITEM_CATEGORIES = (
    "tool", "key", "consumable", "evidence", "gift", "quest", "decoration",
    "outfit_piece", "spell")
VALID_RARITIES = ("common", "rare", "unique")
VALID_OBTAIN_METHODS = ("found", "given", "crafted", "purchased", "quest_reward", "manual")

# Body-Slots fuer Outfit-Pieces. Reihenfolge ist die Prompt-Assembly-Reihenfolge.
VALID_PIECE_SLOTS = (
    "underwear_top", "underwear_bottom", "legs",
    "top", "bottom", "outer", "feet", "neck", "head")


# ============================================================
# 1. ITEM-DEFINITIONEN (user-level)
# ============================================================

def _get_items_file() -> Path:
    sd = get_storage_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "items.json"


def _get_item_dir(item_id: str) -> Path:
    item_dir = get_storage_dir() / "items" / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    return item_dir


# Shared-Items (cross-world library)
from app.core.paths import get_shared_dir as _get_shared_dir


def _get_shared_items_file() -> Path:
    sd = _get_shared_dir() / "items"
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "items.json"


def _get_shared_item_dir(item_id: str) -> Path:
    d = _get_shared_dir() / "items" / item_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_shared_items() -> List[Dict[str, Any]]:
    path = _get_shared_items_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("items", [])
    except Exception:
        logger.warning("Shared-Items konnten nicht geladen werden")
        return []


def _save_shared_items(items: List[Dict[str, Any]]):
    path = _get_shared_items_file()
    path.write_text(
        json.dumps({"items": items, "last_updated": datetime.now().isoformat()},
                    ensure_ascii=False, indent=2),
        encoding="utf-8")


def list_shared_items() -> List[Dict[str, Any]]:
    return _load_shared_items()


def get_shared_item(item_id: str) -> Optional[Dict[str, Any]]:
    for it in _load_shared_items():
        if it.get("id") == item_id:
            return it
    return None


def is_shared_item(item_id: str) -> bool:
    return get_shared_item(item_id) is not None


def get_item_image_path(item_id: str) -> str:
    """Liefert den absoluten Pfad zum Item-Bild oder leer wenn nicht vorhanden.

    Sucht erst im World-Item-Dir, dann (falls Shared-Item) im shared/items-Dir.
    """
    item = get_item(item_id)
    if not item:
        return ""
    filename = (item.get("image") or "").strip() or "image.png"
    if item.get("_shared"):
        candidate = _get_shared_item_dir(item_id) / filename
    else:
        candidate = _get_item_dir(item_id) / filename
    return str(candidate) if candidate.exists() else ""


def _load_items() -> List[Dict[str, Any]]:
    """Laedt alle Item-Definitionen aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, name, category, prompt_fragment, pieces, slots, meta, "
            "created_at, updated_at FROM items ORDER BY name ASC"
        ).fetchall()
        items = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[6] or "{}")
            except Exception:
                pass
            item = {
                "id": r[0],
                "name": r[1],
                "category": r[2],
                "prompt_fragment": r[3],
                "created_at": r[7],
                **meta,
            }
            try:
                pieces = json.loads(r[4] or "{}")
                if pieces:
                    item["outfit_piece"] = pieces
            except Exception:
                pass
            try:
                slots = json.loads(r[5] or "[]")
                if slots:
                    item["slots"] = slots
            except Exception:
                pass
            items.append(item)
        return items
    except Exception as e:
        logger.warning("Fehler beim Laden der Items aus DB: %s", e)
        # Fallback: JSON-Datei
        path = _get_items_file()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("items", [])
        except Exception:
            return []


def _save_items(items: List[Dict[str, Any]]):
    """Speichert alle Item-Definitionen in die DB (Upsert)."""
    now = datetime.now().isoformat()
    for item in items:
        item_id = item.get("id", "")
        if not item_id:
            continue
        meta = {k: v for k, v in item.items()
                if k not in ("id", "name", "category", "prompt_fragment",
                             "outfit_piece", "pieces", "slots",
                             "created_at", "updated_at")}
        try:
            with transaction() as conn:
                conn.execute("""
                    INSERT INTO items (id, name, category, prompt_fragment,
                        pieces, slots, meta, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        category=excluded.category,
                        prompt_fragment=excluded.prompt_fragment,
                        pieces=excluded.pieces,
                        slots=excluded.slots,
                        meta=excluded.meta,
                        updated_at=excluded.updated_at
                """, (
                    item_id,
                    item.get("name", ""),
                    item.get("category", ""),
                    item.get("prompt_fragment", item.get("image_prompt", "")),
                    json.dumps(item.get("outfit_piece", item.get("pieces", {})),
                               ensure_ascii=False),
                    json.dumps(item.get("slots", []), ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False),
                    item.get("created_at", now),
                    now,
                ))
        except Exception as e:
            logger.error("_save_items Fehler fuer %s: %s", item_id, e)


def list_items() -> List[Dict[str, Any]]:
    """Listet alle Item-Definitionen."""
    return _load_items()


def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    """Gibt ein einzelnes Item zurueck. Sucht erst in den World-Items,
    dann in den globalen Shared-Items."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT id, name, category, prompt_fragment, pieces, slots, meta, "
            "created_at FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        if row:
            meta = {}
            try:
                meta = json.loads(row[6] or "{}")
            except Exception:
                pass
            item = {
                "id": row[0],
                "name": row[1],
                "category": row[2],
                "prompt_fragment": row[3],
                "created_at": row[7],
                **meta,
            }
            try:
                pieces = json.loads(row[4] or "{}")
                if pieces:
                    item["outfit_piece"] = pieces
            except Exception:
                pass
            return item
    except Exception as e:
        logger.debug("get_item DB-Fehler fuer %s: %s", item_id, e)

    # Fallback: in-memory list
    for item in _load_items():
        if item.get("id") == item_id:
            return item

    shared = get_shared_item(item_id)
    if shared:
        return {**shared, "_shared": True}
    return None


def resolve_item_id(token: str) -> Optional[str]:
    """Loest einen Token (ID, Name oder 'item_<Name>') zur echten Item-ID auf.

    Reihenfolge: exakte ID → exakter Name (case-insensitive) → Token ohne
    'item_'-Prefix als Name. Gibt None zurueck, wenn nichts passt.
    """
    if not token:
        return None
    token = token.strip()
    items = _load_items()

    for item in items:
        if item.get("id") == token:
            return token

    token_lower = token.lower()
    for item in items:
        if (item.get("name") or "").lower() == token_lower:
            return item.get("id")

    if token_lower.startswith("item_"):
        stripped = token_lower[5:]
        for item in items:
            if (item.get("name") or "").lower() == stripped:
                return item.get("id")

    return None


def _clean_piece_slots(raw) -> List[str]:
    """Normalisiert die Slot-Liste eines Outfit-Pieces.

    Symmetrisch — kein primary/additional. Ein Piece belegt alle Slots
    in der Liste gleichberechtigt.

    - Nur gueltige Slots aus VALID_PIECE_SLOTS
    - Dedup, Reihenfolge stabil
    - Leere Liste ist erlaubt (Aufrufer entscheidet ob das ein Fehler ist)
    """
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    result: List[str] = []
    seen = set()
    for s in raw:
        if not isinstance(s, str):
            continue
        slot = s.strip().lower()
        if not slot:
            continue
        if slot not in VALID_PIECE_SLOTS:
            continue
        if slot in seen:
            continue
        seen.add(slot)
        result.append(slot)
    return result


def _piece_slots(item: Optional[Dict[str, Any]]) -> List[str]:
    """Extrahiert die slot-Liste eines Items. Liefert [] wenn kein outfit_piece."""
    if not item:
        return []
    op = item.get("outfit_piece") or {}
    return list(op.get("slots") or [])


def _piece_render_slot(item_slots: List[str]) -> str:
    """Bestimmt deterministisch den Render-Slot eines Multi-Slot-Pieces.

    Render-Reihenfolge folgt VALID_PIECE_SLOTS — der erste Slot des Pieces
    in dieser Reihenfolge ist der Render-Slot. Dadurch wird das Fragment
    nur einmal in den Image-Prompt aufgenommen.
    """
    sset = set(item_slots or [])
    for s in VALID_PIECE_SLOTS:
        if s in sset:
            return s
    return ""


def _clean_piece_lora(raw) -> Dict[str, Any]:
    """Normalisiert das LoRA eines Outfit-Pieces (maximal EINS pro Piece).

    Schema: {"name": str, "strength": float, "workflow": str}.
    workflow="" heisst universell (passt zu allen Workflows).
    Accepts dict (new format) oder list (legacy — nimmt ersten Eintrag).
    """
    entry = raw
    if isinstance(raw, list):
        entry = raw[0] if raw else None
    if not isinstance(entry, dict):
        return {}
    nm = (entry.get("name") or "").strip()
    if not nm or nm.lower() == "none":
        return {}
    try:
        st = float(entry.get("strength", 1.0))
    except Exception:
        st = 1.0
    wf = (entry.get("workflow") or entry.get("model") or "").strip()
    return {"name": nm, "strength": st, "workflow": wf}


def add_item(name: str,
    description: str = "",
    category: str = "tool",
    image_prompt: str = "",
    rarity: str = "common",
    stackable: bool = False,
    max_stack: int = 1,
    properties: Optional[Dict[str, Any]] = None,
    transferable: bool = True,
    consumable: bool = False,
    reveals_secret: Optional[Dict[str, str]] = None,
    prompt_fragment: str = "",
    outfit_piece: Optional[Dict[str, Any]] = None,
    effects: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Erstellt ein neues Item.

    prompt_fragment: Text-Baustein fuer die Bild-Prompt-Assembly, wenn das
        Item equipped/angelegt ist. Beispiel: "holding a hammer in the hand".
    outfit_piece: Nur fuer category=outfit_piece. Dict mit Feldern wie
        {"slots": ["top", "bottom"], "covers": ["underwear_top"]}. slots ist
        eine symmetrische Liste — kein Primary-/Additional-Konzept mehr.
    """
    if category not in VALID_ITEM_CATEGORIES:
        category = "tool"
    if rarity not in VALID_RARITIES:
        rarity = "common"

    items = _load_items()
    item = {
        "id": f"item_{uuid.uuid4().hex[:8]}",
        "name": name.strip(),
        "description": description.strip(),
        "category": category,
        "image": None,
        "image_prompt": image_prompt.strip(),
        "prompt_fragment": (prompt_fragment or "").strip(),
        "rarity": rarity,
        "stackable": stackable,
        "max_stack": max(1, max_stack),
        "transferable": bool(transferable),
        "consumable": bool(consumable),
        "reveals_secret": reveals_secret if isinstance(reveals_secret, dict) else None,
        "effects": effects if isinstance(effects, dict) and effects else None,
        "properties": properties or {
            "enables_locations": [],
            "enables_activities": [],
            # stat_bonuses: generisches Dict wie {"stamina": 5, "courage": 10}.
            # Keine hartcodierten Stat-Namen — jeder im Template definierte Stat kann
            # hier vergeben werden wenn Item-Gameplay implementiert wird.
            "stat_bonuses": {},
            "uses": None,
        },
        "created_at": datetime.now().isoformat(),
    }
    # Piece-spezifisches Sub-Dict nur setzen wenn Kategorie passt
    if category == "outfit_piece" and outfit_piece:
        slots = _clean_piece_slots(outfit_piece.get("slots"))
        if not slots:
            raise ValueError(
                f"outfit_piece needs non-empty 'slots' list (valid: {VALID_PIECE_SLOTS})")
        item["outfit_piece"] = {
            "slots": slots,
            "covers": [s for s in _clean_piece_slots(outfit_piece.get("covers")) if s not in slots],
            "partially_covers": [s for s in _clean_piece_slots(outfit_piece.get("partially_covers")) if s not in slots],
            "outfit_types": [s.strip() for s in (outfit_piece.get("outfit_types") or []) if s and s.strip()],
            "lora": _clean_piece_lora(outfit_piece.get("lora") or outfit_piece.get("loras")),
        }

    items.append(item)
    _save_items(items)
    logger.info("Item erstellt: %s '%s' (category=%s, rarity=%s)",
                item["id"], name, category, rarity)
    return item


def update_item(item_id: str,
    updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Aktualisiert ein Item — World oder Shared. Admin-only Aufrufer.
    """
    allowed = {
        "name", "description",
        "category", "image", "image_prompt",
        "rarity", "stackable", "max_stack", "properties",
        "transferable", "consumable", "reveals_secret",
        "prompt_fragment", "outfit_piece", "effects",
        # Magic / spell metadata (siehe spell_engine.build_spell_catalog)
        "incantation", "spell_mode", "clone_item_id",
        "success_chance", "copy_on_give",
        "success_text", "fail_text", "cast_activity",
        "anchor_item_id", "teleport_subject",
        # Tracker: while carried, the carrier sees the target's location in the prompt.
        "tracks_character",
    }

    def _apply_updates(item: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in updates.items():
            if key in allowed:
                item[key] = value
        if item.get("category") not in VALID_ITEM_CATEGORIES:
            item["category"] = "tool"
        if item.get("rarity") not in VALID_RARITIES:
            item["rarity"] = "common"
        if item.get("category") == "outfit_piece":
            op = item.get("outfit_piece") or {}
            slots = _clean_piece_slots(op.get("slots"))
            if not slots:
                raise ValueError(
                    f"outfit_piece needs non-empty 'slots' list (valid: {VALID_PIECE_SLOTS})")
            item["outfit_piece"] = {
                "slots": slots,
                "covers": [s for s in _clean_piece_slots(op.get("covers")) if s not in slots],
                "partially_covers": [s for s in _clean_piece_slots(op.get("partially_covers")) if s not in slots],
                "outfit_types": [s.strip() for s in (op.get("outfit_types") or []) if s and s.strip()],
                "lora": _clean_piece_lora(op.get("lora")),
            }
        else:
            item.pop("outfit_piece", None)
        return item

    # Shared-Items: in shared/items/items.json schreiben
    if is_shared_item(item_id):
        shared = _load_shared_items()
        for idx, item in enumerate(shared):
            if item.get("id") == item_id:
                # _shared-Flag ist Run-Time-Marker, nicht Teil der Persistenz.
                item.pop("_shared", None)
                shared[idx] = _apply_updates(item)
                _save_shared_items(shared)
                logger.info("Shared-Item aktualisiert: %s", item_id)
                return {**shared[idx], "_shared": True}
        return None

    # World-Items: DB / items.json
    items = _load_items()
    for item in items:
        if item.get("id") == item_id:
            _apply_updates(item)
            _save_items(items)
            logger.info("Item aktualisiert: %s", item_id)
            return item
    return None


def delete_item(item_id: str) -> bool:
    """Loescht ein Item und entfernt es aus allen Raeumen und Inventaren.
    Funktioniert fuer World- und Shared-Items.
    """
    # Shared-Items: aus shared/items/items.json + Bild-Verzeichnis loeschen.
    # Inventar-Bezuege koennen nicht entfernt werden — Shared-Items kennen keine
    # einzelne Welt; jede betroffene Welt erhaelt 'phantom' Item-Refs (haben dort
    # ohnehin keine Wirkung mehr, da get_item nichts mehr findet).
    if is_shared_item(item_id):
        shared = _load_shared_items()
        new_shared = [s for s in shared if s.get("id") != item_id]
        if len(new_shared) == len(shared):
            return False
        _save_shared_items(new_shared)
        item_dir = _get_shared_item_dir(item_id)
        if item_dir.exists():
            import shutil
            shutil.rmtree(item_dir, ignore_errors=True)
        logger.info("Shared-Item geloescht: %s", item_id)
        return True
    items = _load_items()
    new_items = [i for i in items if i.get("id") != item_id]
    if len(new_items) < len(items):
        # Explizit aus der DB entfernen — _save_items ist nur UPSERT.
        try:
            with transaction() as conn:
                conn.execute("DELETE FROM items WHERE id=?", (item_id,))
                # Auch aus Inventaren + Raum-Ablage entfernen
                conn.execute("DELETE FROM inventory_items WHERE item_id=?", (item_id,))
        except Exception as e:
            logger.error("delete_item DB-Fehler fuer %s: %s", item_id, e)
        _save_items(new_items)
        # Bild-Verzeichnis loeschen
        item_dir = _get_item_dir(item_id)
        if item_dir.exists():
            import shutil
            shutil.rmtree(item_dir, ignore_errors=True)
        logger.info("Item geloescht: %s", item_id)
        return True
    return False


def move_item_to_shared(item_id: str) -> Dict[str, Any]:
    """Verschiebt ein World-Item in die Shared-Library (inkl. Bild-Verzeichnis).

    Returns {"status": "ok"|"error", "reason"?}
    """
    import shutil
    if is_shared_item(item_id):
        return {"status": "error", "reason": "already_shared"}
    items = _load_items()
    target = None
    for it in items:
        if it.get("id") == item_id:
            target = it
            break
    if not target:
        return {"status": "error", "reason": "not_found"}

    # 1) Shared-Liste aktualisieren
    shared = _load_shared_items()
    shared = [s for s in shared if s.get("id") != item_id]
    shared.append(target)
    _save_shared_items(shared)

    # 2) Bild-Verzeichnis uebertragen (copy, dann source loeschen)
    src = _get_item_dir(item_id)
    dst = _get_shared_item_dir(item_id)
    if src.exists():
        for child in src.iterdir():
            _tgt = dst / child.name
            if _tgt.exists():
                _tgt.unlink()
            shutil.move(str(child), str(_tgt))
        try:
            src.rmdir()
        except OSError:
            pass

    # 3) Aus der World entfernen — _save_items ist UPSERT, also explizit DELETE.
    # Sonst bleibt der DB-Eintrag haengen und get_item liefert das Item
    # weiter als World-Item zurueck (Bild-Pfad falsch, Move-Buttons falsch).
    try:
        with transaction() as conn:
            conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    except Exception as e:
        logger.error("move_item_to_shared DB-DELETE Fehler fuer %s: %s", item_id, e)
    _save_items([it for it in items if it.get("id") != item_id])
    logger.info("Item %s -> shared library verschoben", item_id)
    return {"status": "ok", "item_id": item_id}


def move_item_to_world(item_id: str) -> Dict[str, Any]:
    """Verschiebt ein Shared-Item in die World des Users (inkl. Bild)."""
    import shutil
    shared_item = get_shared_item(item_id)
    if not shared_item:
        return {"status": "error", "reason": "not_in_shared"}
    items = _load_items()
    if any(it.get("id") == item_id for it in items):
        return {"status": "error", "reason": "already_in_world"}

    # 1) In World-Liste einfuegen (exact copy, kein _shared-Flag)
    items.append({k: v for k, v in shared_item.items() if k != "_shared"})
    _save_items(items)

    # 2) Bild-Verzeichnis uebertragen
    src = _get_shared_item_dir(item_id)
    dst = _get_item_dir(item_id)
    if src.exists():
        for child in src.iterdir():
            _tgt = dst / child.name
            if _tgt.exists():
                _tgt.unlink()
            shutil.move(str(child), str(_tgt))
        try:
            src.rmdir()
        except OSError:
            pass

    # 3) Aus Shared entfernen
    _save_shared_items([s for s in _load_shared_items() if s.get("id") != item_id])
    logger.info("Item %s -> world (%s) verschoben", item_id)
    return {"status": "ok", "item_id": item_id}


def set_item_image(item_id: str, image_filename: str) -> bool:
    """Setzt das Bild eines Items (World oder Shared)."""
    if is_shared_item(item_id):
        shared = _load_shared_items()
        for item in shared:
            if item.get("id") == item_id:
                item["image"] = image_filename
                _save_shared_items(shared)
                return True
        return False
    items = _load_items()
    for item in items:
        if item.get("id") == item_id:
            item["image"] = image_filename
            _save_items(items)
            return True
    return False


def set_item_image_meta(item_id: str, meta: Dict[str, Any]) -> bool:
    """Speichert Erzeugungs-Metadaten (backend, model) eines Item-Bildes.

    Analog zu set_gallery_image_meta fuer Ortsbilder — wird vom
    Generate-Dialog im Game-Admin als Caption unter dem Bild angezeigt.
    """
    if is_shared_item(item_id):
        shared = _load_shared_items()
        for item in shared:
            if item.get("id") == item_id:
                item["image_meta"] = dict(meta)
                _save_shared_items(shared)
                return True
        return False
    items = _load_items()
    for item in items:
        if item.get("id") == item_id:
            item["image_meta"] = dict(meta)
            _save_items(items)
            return True
    return False


# ============================================================
# 2. RAUM-ITEMS (in world.json eingebettet)
# ============================================================

def get_room_items(location_id: str, room_id: str) -> List[Dict[str, Any]]:
    """Gibt Items in einem Raum zurueck."""
    from app.models.world import get_location_by_id, get_room_by_id
    location = get_location_by_id(location_id)
    if not location:
        return []
    room = get_room_by_id(location, room_id)
    if not room:
        return []
    return room.get("items", [])


def add_item_to_room(location_id: str,
    room_id: str,
    item_id: str,
    quantity: int = 1,
    hidden: bool = False,
    discovery_difficulty: int = 0,
    note: str = "") -> bool:
    """Platziert ein Item in einem Raum."""
    from app.models.world import list_locations

    # Item existiert?
    item = get_item(item_id)
    if not item:
        return False

    locations = list_locations()
    for loc in locations:
        if loc.get("id") == location_id:
            for room in loc.get("rooms", []):
                if room.get("id") == room_id:
                    room_items = room.get("items", [])
                    # Bereits vorhanden? Quantity erhoehen
                    for ri in room_items:
                        if ri.get("item_id") == item_id:
                            ri["quantity"] = ri.get("quantity", 1) + quantity
                            ri["hidden"] = hidden
                            ri["discovery_difficulty"] = discovery_difficulty
                            if note:
                                ri["note"] = note
                            _save_locations(locations)
                            return True
                    # Neu hinzufuegen
                    room_items.append({
                        "item_id": item_id,
                        "quantity": max(1, quantity),
                        "hidden": hidden,
                        "discovery_difficulty": max(0, min(5, discovery_difficulty)),
                        "note": note.strip(),
                    })
                    room["items"] = room_items
                    _save_locations(locations)
                    logger.info("Item %s in Raum %s/%s platziert", item_id, location_id, room_id)
                    return True
    return False


def remove_item_from_room(location_id: str,
    room_id: str,
    item_id: str,
    quantity: int = 1) -> bool:
    """Entfernt ein Item (oder reduziert Quantity) aus einem Raum."""
    from app.models.world import list_locations

    locations = list_locations()
    for loc in locations:
        if loc.get("id") == location_id:
            for room in loc.get("rooms", []):
                if room.get("id") == room_id:
                    room_items = room.get("items", [])
                    for ri in room_items:
                        if ri.get("item_id") == item_id:
                            ri["quantity"] = ri.get("quantity", 1) - quantity
                            if ri["quantity"] <= 0:
                                room_items.remove(ri)
                            room["items"] = room_items
                            _save_locations(locations)
                            logger.info("Item %s aus Raum %s/%s entfernt", item_id, location_id, room_id)
                            return True
    return False


def _save_locations(locations: List[Dict[str, Any]]):
    """Speichert Locations (inkl. Raum-Items) in die DB.

    Delegiert an `_save_world_data` in world.py — Welt liegt komplett
    in den `locations`/`rooms`-Tabellen.
    """
    from app.models.world import _save_world_data
    _save_world_data({"locations": locations})


def find_item_location(item_id: str,
                        exclude_character: str = "") -> Optional[Dict[str, Any]]:
    """Findet wo ein Item aktuell physisch liegt — Anker-Lookup fuer
    Teleport-Spells.

    Sucht in dieser Reihenfolge:
        1. Character-Inventare (inventory_items table)
        2. Raum-Items (locations.rooms[].items)

    ``exclude_character`` filtert einen Character-Inhaber heraus — wird
    vom Spell-Engine genutzt, damit ein Caster nicht "zu sich selbst"
    teleportiert wenn er zufaellig auch den Anker traegt.

    Returns:
        - {"kind": "character", "character": <name>} wenn Item im Inventar
        - {"kind": "room", "location_id": <id>, "room_id": <id>} wenn im Raum
        - None wenn nirgends gefunden
    """
    if not item_id:
        return None
    # 1) Character-Inventar
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT character_name FROM inventory_items WHERE item_id=?",
            (item_id,),
        ).fetchall()
        for r in rows:
            holder = (r[0] or "").strip()
            if not holder:
                continue
            if exclude_character and holder == exclude_character:
                continue
            return {"kind": "character", "character": holder}
    except Exception as e:
        logger.debug("find_item_location DB-Fehler: %s", e)
    # 2) Raum-Items
    try:
        from app.models.world import list_locations
        for loc in list_locations() or []:
            for room in loc.get("rooms", []) or []:
                for ri in room.get("items", []) or []:
                    if ri.get("item_id") == item_id:
                        return {
                            "kind": "room",
                            "location_id": loc.get("id") or "",
                            "room_id": room.get("id") or "",
                        }
    except Exception as e:
        logger.debug("find_item_location room-scan failed: %s", e)
    return None


# ============================================================
# 3. CHARACTER-INVENTAR
# ============================================================

def _get_inventory_file(character_name: str) -> Path:
    char_dir = get_storage_dir() / "characters" / character_name
    char_dir.mkdir(parents=True, exist_ok=True)
    return char_dir / "inventory.json"


def _load_inventory(character_name: str) -> Dict[str, Any]:
    """Laedt das Inventar eines Characters aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT item_id, quantity, acquired_at, meta "
            "FROM inventory_items WHERE character_name=? ORDER BY acquired_at ASC",
            (character_name,),
        ).fetchall()
        inventory = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[3] or "{}")
            except Exception:
                pass
            entry = {
                "item_id": r[0],
                "quantity": r[1] or 1,
                "obtained_at": r[2] or "",
                **meta,
            }
            inventory.append(entry)
        # max_slots aus character_state holen wenn vorhanden
        max_slots = 20
        try:
            row = conn.execute(
                "SELECT state_json FROM character_state WHERE character_name=?",
                (character_name,),
            ).fetchone()
            if row and row[0]:
                st = json.loads(row[0])
                max_slots = int(st.get("max_inventory_slots", 20))
        except Exception:
            pass
        return {"inventory": inventory, "max_slots": max_slots, "last_updated": None}
    except Exception as e:
        logger.warning("_load_inventory DB-Fehler fuer %s: %s", character_name, e)
        # Fallback: JSON-Datei
        path = _get_inventory_file(character_name)
        if not path.exists():
            return {"inventory": [], "max_slots": 20, "last_updated": None}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
        except Exception:
            return {"inventory": [], "max_slots": 20, "last_updated": None}


def _save_inventory(character_name: str, data: Dict[str, Any]):
    """Speichert das Inventar eines Characters in die DB (Upsert)."""
    now = datetime.now().isoformat()
    inventory = data.get("inventory", [])
    try:
        with transaction() as conn:
            # Bestehende Eintraege holen um Diffs zu berechnen
            existing_ids = {r[0] for r in conn.execute(
                "SELECT item_id FROM inventory_items WHERE character_name=?",
                (character_name,),
            ).fetchall()}
            new_ids = {e.get("item_id") for e in inventory if e.get("item_id")}

            # Geloeschte Eintraege entfernen
            for iid in existing_ids - new_ids:
                conn.execute(
                    "DELETE FROM inventory_items WHERE character_name=? AND item_id=?",
                    (character_name, iid),
                )

            # Upsert fuer alle aktuellen Eintraege
            for entry in inventory:
                iid = entry.get("item_id")
                if not iid:
                    continue
                # Meta: alle Keys ausser Kern-Felder
                meta = {k: v for k, v in entry.items()
                        if k not in ("item_id", "quantity", "obtained_at")}
                conn.execute("""
                    INSERT INTO inventory_items
                        (character_name, item_id, quantity, acquired_at, meta)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(character_name, item_id) DO UPDATE SET
                        quantity=excluded.quantity,
                        acquired_at=excluded.acquired_at,
                        meta=excluded.meta
                """, (
                    character_name,
                    iid,
                    entry.get("quantity", 1),
                    entry.get("obtained_at", now),
                    json.dumps(meta, ensure_ascii=False),
                ))
    except Exception as e:
        logger.error("_save_inventory DB-Fehler fuer %s: %s", character_name, e)


def get_character_inventory(character_name: str,
                            include_equipped: bool = True) -> Dict[str, Any]:
    """Gibt das vollstaendige Inventar eines Characters zurueck.

    Enriched die Eintraege mit Item-Details (Name, Beschreibung, Bild,
    Outfit-Piece-Slot, prompt_fragment).

    include_equipped: wenn False, werden equipped Pieces und equipped Items
        herausgefiltert (fuer die Standard-Inventar-Anzeige).
    """
    data = _load_inventory(character_name)
    inventory = data.get("inventory", [])

    # Equipped-Set fuer Filter / Marker
    equipped_set = set(get_equipped_item_ids(character_name))

    # Enrichment: Item-Details hinzufuegen
    enriched = []
    phantom_ids: List[str] = []
    for entry in inventory:
        iid = entry.get("item_id", "")
        if not include_equipped and iid in equipped_set:
            continue
        item = get_item(iid)
        if not item:
            # Phantom: Item-Definition geloescht aber Inventar-Eintrag noch da.
            # Nicht in der UI zeigen + zur Auto-Cleanup-Liste merken.
            phantom_ids.append(iid)
            continue
        enriched_entry = {**entry}
        enriched_entry["item_name"] = item.get("name", "?")
        enriched_entry["item_description"] = item.get("description", "")
        enriched_entry["item_category"] = item.get("category", "")
        enriched_entry["item_image"] = item.get("image")
        enriched_entry["item_rarity"] = item.get("rarity", "common")
        enriched_entry["item_consumable"] = bool(item.get("consumable", False))
        enriched_entry["item_transferable"] = bool(item.get("transferable", True))
        enriched_entry["item_prompt_fragment"] = item.get("prompt_fragment", "")
        enriched_entry["item_image_prompt"] = item.get("image_prompt", "")
        enriched_entry["item_incantation"] = item.get("incantation", "")
        enriched_entry["_shared"] = bool(item.get("_shared", False))
        if item.get("outfit_piece"):
            enriched_entry["outfit_piece"] = item["outfit_piece"]
        enriched_entry["equipped"] = (iid in equipped_set)
        enriched.append(enriched_entry)

    # Phantom-Eintraege wegraeumen — passieren z.B. wenn Items in Game Admin
    # geloescht werden ohne die Inventare zu cleanen. Best-effort, Fehler
    # ignoriert: ein Lese-Endpoint soll nie an einer Mutation scheitern.
    if phantom_ids:
        try:
            with transaction() as conn:
                for iid in phantom_ids:
                    conn.execute(
                        "DELETE FROM inventory_items WHERE character_name=? AND item_id=?",
                        (character_name, iid))
                logger.info("Phantom-Inventareintraege fuer %s entfernt: %s",
                            character_name, phantom_ids)
        except Exception as e:
            logger.debug("Phantom-Cleanup fehlgeschlagen fuer %s: %s",
                         character_name, e)

    return {
        "inventory": enriched,
        "max_slots": data.get("max_slots", 20),
        "last_updated": data.get("last_updated"),
    }


def add_to_inventory(character_name: str,
    item_id: str,
    quantity: int = 1,
    obtained_from: str = "",
    obtained_method: str = "manual",
    locked: Optional[bool] = None) -> bool:
    """Fuegt ein Item zum Character-Inventar hinzu.

    locked: wenn True, das Item kann nicht entfernt/abgegeben werden ohne
        admin force-removal. Default None heisst: kein Lock.

    Effekte (apply_condition, stat_changes, mood_influence) feuern NICHT
    beim Hinzufuegen — nur ueber explizites :func:`consume_item`. Damit
    laesst sich ein Trank im Game Admin oder per Geschenk uebergeben ohne
    dass die Wirkung sofort eintritt; der Empfaenger entscheidet selbst
    wann er ihn anwendet (oder das Force-Cast-Pattern in :func:`give_item`
    konsumiert direkt nach dem Add).
    """
    item = get_item(item_id)
    if not item:
        return False

    if obtained_method not in VALID_OBTAIN_METHODS:
        obtained_method = "manual"

    # Locked-Flag — nur per Call-Override (z.B. give_item mode=force).
    # Item-seitige Default-Bindung wurde entfernt.
    if locked is None:
        locked = False

    data = _load_inventory(character_name)
    inventory = data.get("inventory", [])

    # Stackable? Bestehenden Eintrag erhoehen — aber nicht wenn locked
    # gesetzt ist (force-cast braucht eigene Instanz).
    if item.get("stackable") and not locked:
        for entry in inventory:
            if entry.get("item_id") == item_id:
                max_stack = item.get("max_stack", 99)
                entry["quantity"] = min(entry.get("quantity", 1) + quantity, max_stack)
                _save_inventory(character_name, data)
                return True

    # Slot-Limit pruefen — Outfit-Pieces sind Garderobe und zaehlen nicht mit
    if item.get("category") != "outfit_piece":
        max_slots = data.get("max_slots", 20)
        non_piece_count = 0
        for _e in inventory:
            _it = get_item(_e.get("item_id", ""))
            if _it and _it.get("category") != "outfit_piece":
                non_piece_count += 1
        if non_piece_count >= max_slots:
            logger.warning("Inventar voll fuer %s (%d/%d)", character_name, non_piece_count, max_slots)
            return False

    # Neuer Eintrag
    new_entry: Dict[str, Any] = {
        "item_id": item_id,
        "quantity": max(1, quantity),
        "obtained_at": datetime.now().isoformat(),
        "obtained_from": obtained_from,
        "obtained_method": obtained_method,
        "equipped": False,
        "notes": "",
    }
    if locked:
        new_entry["locked"] = True
    inventory.append(new_entry)
    data["inventory"] = inventory
    _save_inventory(character_name, data)
    logger.info("Item %s zum Inventar von %s hinzugefuegt%s",
                item_id, character_name,
                " (locked)" if locked else "")

    # apply_condition / Effects fliessen NICHT beim Hinzufuegen — nur via
    # consume_item. Force-Cast in give_item ruft consume_item explizit
    # auf, gifted/manual Items bleiben passiv im Inventar bis der Owner
    # sie konsumiert.

    # Secret-Reveal-Trigger: Wenn Item ein Geheimnis enthuellt + nicht via "manual" admin
    # (manual=Admin-UI direkt zugewiesen, kein narrativer Pickup)
    if obtained_method != "manual":
        _trigger_secret_reveal(item, character_name)

    return True


def give_item(from_character: str,
              to_character: str,
              item_id: str,
              mode: str = "gift",
              consume_source: bool = False) -> bool:
    """Transfer-Verb: legt das Item beim Ziel an, optional als Magie-Effekt.

    mode:
      - ``gift``   — normales Geben. Item geht ins Inventar des Ziels und
                     bleibt dort liegen — Empfaenger entscheidet ob/wann
                     er es konsumiert.
      - ``force``  — fuer Magie/Zauber: Item wird beim Ziel SOFORT
                     konsumiert. effects (stat changes + apply_condition)
                     greifen direkt; das Item taucht nur kurz im Inventar
                     auf und ist dann weg. Kein Lock noetig.

    consume_source: wenn True und ``from_character`` das Item besitzt, wird
        bei ihm 1 Stueck abgezogen (Schriftrolle/Trank verbraucht sich).
    """
    if not item_id or not to_character:
        return False

    if consume_source and from_character:
        try:
            remove_from_inventory(from_character, item_id, quantity=1, force=True)
        except TypeError:
            # Fallback falls force-Param noch nicht da ist
            remove_from_inventory(from_character, item_id, quantity=1)

    is_force = mode == "force"
    ok = add_to_inventory(
        character_name=to_character,
        item_id=item_id,
        quantity=1,
        obtained_from=from_character or "",
        obtained_method="given",
    )
    if not ok:
        return False

    if is_force:
        # Sofort beim Empfaenger konsumieren — Effects + Condition feuern,
        # Item verschwindet aus dem Inventar. Damit ist die Magie-Wirkung
        # direkt aktiv und es bleibt kein "geisterndes" Item liegen.
        try:
            consume_item(to_character, item_id)
        except Exception as e:
            logger.error("Force-Cast consume failed for %s/%s: %s",
                         to_character, item_id, e)
            return False
    return True


def remove_from_inventory(character_name: str,
    item_id: str,
    quantity: int = 1,
    force: bool = False) -> bool:
    """Entfernt ein Item aus dem Character-Inventar.

    force: wenn True, wird auch ein ``locked`` markiertes Item entfernt
    (z.B. fuer Cleanup-Job nach TTL-Ablauf, oder Story-Engine-Override).
    Standard False: locked Items koennen vom Owner nicht abgelegt werden.
    """
    data = _load_inventory(character_name)
    inventory = data.get("inventory", [])

    for entry in inventory:
        if entry.get("item_id") == item_id:
            if entry.get("locked") and not force:
                logger.info("Item %s ist locked auf %s — Entfernen abgelehnt",
                            item_id, character_name)
                return False
            entry["quantity"] = entry.get("quantity", 1) - quantity
            if entry["quantity"] <= 0:
                inventory.remove(entry)
            data["inventory"] = inventory
            _save_inventory(character_name, data)
            logger.info("Item %s aus Inventar von %s entfernt", item_id, character_name)
            return True
    return False




def update_inventory_entry(character_name: str,
    item_id: str,
    updates: Dict[str, Any]) -> bool:
    """Aktualisiert einen Inventar-Eintrag (equipped, notes)."""
    data = _load_inventory(character_name)
    inventory = data.get("inventory", [])

    for entry in inventory:
        if entry.get("item_id") == item_id:
            allowed = {"equipped", "notes", "quantity"}
            for key, value in updates.items():
                if key in allowed:
                    entry[key] = value
            _save_inventory(character_name, data)
            return True
    return False


def find_inventory_piece_by_name_slot(character_name: str,
    name: str,
    slot: str,
    prompt_fragment: str = "") -> Optional[str]:
    """Sucht im Character-Inventar ein outfit_piece mit matchendem Slot.

    Match-Reihenfolge (alle case-insensitive, getrimmt):
      1. Name-Match: exakt gleich (klassischer Pfad).
      2. Prompt-Fragment-Match: falls prompt_fragment uebergeben — Substring
         in beide Richtungen ('white blouse' ist in 'white blouse with deep
         neckline, silk' enthalten und umgekehrt). Dedupe-Schutz gegen
         LLM-Naming-Drift wenn der Name leicht variiert ('Deep Cleavage' vs.
         'Cleverage') aber die Bild-Beschreibung praktisch identisch ist.

    Slot-Match: der Slot muss in item.outfit_piece.slots enthalten sein
    (Multi-Slot-Pieces matchen fuer jeden ihrer Slots).
    Liefert die item_id des ersten Treffers oder None.
    """
    if not name or not slot:
        return None
    target_name = name.strip().lower()
    target_frag = (prompt_fragment or "").strip().lower()
    data = _load_inventory(character_name)

    # Pass 1: exakter Name-Match (schnell, keine false positives)
    for entry in data.get("inventory", []) or []:
        iid = entry.get("item_id", "")
        if not iid:
            continue
        item = get_item(iid)
        if not item or item.get("category") != "outfit_piece":
            continue
        if slot not in _piece_slots(item):
            continue
        if (item.get("name") or "").strip().lower() == target_name:
            return iid

    # Pass 2: Prompt-Fragment-Substring-Match (nur wenn fragment uebergeben).
    # Schutz vor zu kurzem Fragment (z.B. "red" matcht zu vieles).
    if target_frag and len(target_frag) >= 12:
        for entry in data.get("inventory", []) or []:
            iid = entry.get("item_id", "")
            if not iid:
                continue
            item = get_item(iid)
            if not item or item.get("category") != "outfit_piece":
                continue
            if slot not in _piece_slots(item):
                continue
            stored = (item.get("prompt_fragment") or "").strip().lower()
            if not stored or len(stored) < 12:
                continue
            if target_frag in stored or stored in target_frag:
                logger.info("Inventory-Dedupe via prompt_fragment: %s reuse %s "
                            "(neu='%s' / alt='%s')",
                            name, iid, target_frag[:60], stored[:60])
                return iid
    return None


def has_item(character_name: str, item_id: str) -> bool:
    """Prueft ob ein Character ein bestimmtes Item besitzt."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT 1 FROM inventory_items WHERE character_name=? AND item_id=? LIMIT 1",
            (character_name, item_id),
        ).fetchone()
        if row is not None:
            return True
        # Fallback: JSON
    except Exception:
        pass
    data = _load_inventory(character_name)
    return any(e.get("item_id") == item_id for e in data.get("inventory", []))


# --- Item-Verhalten: Konsum, Transfer, Geheimnis-Enthuellung ---

# rarity → strength_delta beim Verschenken
GIFT_RELATIONSHIP_BOOST = {
    "generic": 1,
    "common": 5,
    "rare": 10,
    "unique": 20,
}


def _trigger_secret_reveal(item: Dict[str, Any], knower: str) -> None:
    """Wenn das Item ein Geheimnis enthuellt, markiere es als bekannt."""
    reveals = item.get("reveals_secret") or {}
    if not isinstance(reveals, dict):
        return
    owner = (reveals.get("owner") or "").strip()
    secret_id = (reveals.get("secret_id") or "").strip()
    if not owner or not secret_id:
        return
    if owner == knower:
        return  # Eigenes Geheimnis kann man nicht "entdecken"
    try:
        from app.models.secrets import add_known_by
        add_known_by(owner, secret_id, knower, discovered=True)
        logger.info("Item %s enthuellt Geheimnis %s/%s an %s",
                    item.get("id"), owner, secret_id, knower)
    except Exception as e:
        logger.warning("Secret-Reveal-Trigger fehlgeschlagen: %s", e)


def apply_item_effects(character_name: str, item_id: str) -> Dict[str, Any]:
    """Wendet die effects + apply_condition eines Items auf einen Character
    an, OHNE das Inventar zu beruehren.

    Wird von consume_item aufgerufen NACH dem qty-Decrement, und vom
    Self-Cast-Pfad in spell_engine.execute_cast statt der give_item-Dance
    (der durch UNIQUE(character_name, item_id) korrumpiert).

    Returns: {"success": bool, "changes": dict, "condition_applied": str|None}
    """
    item = get_item(item_id)
    if not item:
        return {"success": False, "changes": {}, "condition_applied": None}
    changes: Dict[str, Any] = {}
    condition_applied: Optional[str] = None
    effects = item.get("effects") or {}
    if isinstance(effects, dict) and effects:
        try:
            from app.core.activity_engine import apply_effects
            # apply_condition / condition_duration_hours sind item-spezifische Felder —
            # apply_effects ignoriert sie (verarbeitet nur *_change und mood_influence).
            changes = apply_effects(character_name, effects, source=f"item:{item.get('name', item_id)}")
        except Exception as e:
            logger.warning("Effects fuer Item %s anwenden fehlgeschlagen: %s", item_id, e)

        cond_name = (effects.get("apply_condition") or "").strip()
        if cond_name:
            try:
                from app.models.character import get_character_profile, save_character_profile, _record_state_change
                profile = get_character_profile(character_name)
                active = profile.get("active_conditions", [])
                if not any(c.get("name") == cond_name for c in active):
                    duration = int(effects.get("condition_duration_hours") or 2)
                    active.append({
                        "name": cond_name,
                        "source": f"item:{item.get('name', item_id)}",
                        "started_at": datetime.now().isoformat(),
                        "duration_hours": max(1, duration),
                    })
                    profile["active_conditions"] = active
                    save_character_profile(character_name, profile)
                    _record_state_change(character_name, "condition", cond_name,
                                          metadata={"source": f"item:{item_id}",
                                                    "duration_hours": max(1, duration)})
                    condition_applied = cond_name
                    logger.info("Condition '%s' aktiviert fuer %s (Quelle: item %s)",
                                 cond_name, character_name, item_id)
            except Exception as e:
                logger.warning("Condition-Apply fuer Item %s fehlgeschlagen: %s", item_id, e)
    return {"success": True, "changes": changes, "condition_applied": condition_applied}


def consume_item(character_name: str, item_id: str) -> Dict[str, Any]:
    """Verbraucht ein Item (qty -1, Eintrag entfernt wenn 0) und wendet
    optionale Effects an (gleiches Format wie Activity-Effects).

    Returns: {"success": bool, "changes": dict, "condition_applied": str|None}
    """
    item = get_item(item_id)
    if not item:
        return {"success": False, "changes": {}, "condition_applied": None}
    success = remove_from_inventory(character_name, item_id, quantity=1)
    if not success:
        return {"success": False, "changes": {}, "condition_applied": None}

    logger.info("%s hat Item '%s' verbraucht", character_name, item.get("name", item_id))
    return apply_item_effects(character_name, item_id)


def transfer_item(from_character: str,
    to_character: str,
    item_id: str,
    quantity: int = 1) -> bool:
    """Bewegt ein Item zwischen Character-Inventaren."""
    if from_character == to_character:
        return False
    if not has_item(from_character, item_id):
        return False
    item = get_item(item_id)
    if not item:
        return False
    if not item.get("transferable", True):
        logger.info("Item %s ist nicht uebertragbar", item_id)
        return False
    # Erst zum Ziel hinzufuegen, dann beim Sender entfernen — falls Ziel-Inventar voll, abbrechen
    added = add_to_inventory(to_character, item_id, quantity=quantity,
        obtained_from=from_character, obtained_method="gift")
    if not added:
        return False
    return remove_from_inventory(from_character, item_id, quantity=quantity)


def gift_item(from_character: str,
    to_character: str,
    item_id: str) -> Dict[str, Any]:
    """Verschenkt ein Item: Transfer + Beziehungs-Boost + Memory + ggf. Secret-Reveal.

    Returns: {success, boost, item_name, error?}
    """
    item = get_item(item_id)
    if not item:
        return {"success": False, "error": "Item nicht gefunden"}

    if not transfer_item(from_character, to_character, item_id, quantity=1):
        return {"success": False, "error": "Transfer fehlgeschlagen (Item fehlt, Inventar voll, oder nicht uebertragbar)"}

    rarity = (item.get("rarity") or "common").lower()
    boost = GIFT_RELATIONSHIP_BOOST.get(rarity, 2)

    # Beziehungs-Boost
    try:
        from app.models.relationship import record_interaction
        record_interaction(from_character, to_character,
            interaction_type="gift",
            summary=f"{from_character} schenkt {to_character} '{item.get('name', item_id)}'",
            sentiment_delta_a=0.05,
            sentiment_delta_b=0.15,
            strength_delta=boost)
    except Exception as e:
        logger.warning("Gift-Beziehungs-Update fehlgeschlagen: %s", e)

    # Memory beim Beschenkten
    try:
        from app.models.memory import add_memory
        add_memory(to_character,
            f"{from_character} hat mir '{item.get('name', item_id)}' geschenkt.",
            category="relationship",
            related_characters=[from_character])
    except Exception as e:
        logger.debug("Memory-Eintrag fuer Geschenk fehlgeschlagen: %s", e)

    # Secret-Reveal beim Beschenkten (falls Item evidence-Type)
    _trigger_secret_reveal(item, to_character)

    return {
        "success": True,
        "boost": boost,
        "item_name": item.get("name", item_id),
        "rarity": rarity,
    }


def pick_up_item(character_name: str,
    location_id: str,
    room_id: str,
    item_id: str,
    quantity: int = 1) -> Dict[str, Any]:
    """Character hebt ein Item aus einem Raum auf — Raum -> Inventar.

    Schreibt Memory + Diary-Eintrag beim Character.

    Returns: {success: bool, error?: str, item_name: str}
    """
    item = get_item(item_id)
    if not item:
        return {"success": False, "error": "Item nicht gefunden"}

    # Im Raum vorhanden?
    room_items = get_room_items(location_id, room_id)
    in_room = next((ri for ri in room_items if ri.get("item_id") == item_id), None)
    if not in_room:
        return {"success": False, "error": "Item liegt nicht in diesem Raum"}
    if int(in_room.get("quantity", 1)) < quantity:
        return {"success": False, "error": "Nicht genug im Raum"}

    # Ins Inventar legen (kann fehlschlagen wenn voll)
    if not add_to_inventory(character_name, item_id, quantity=quantity,
        obtained_from=f"{location_id}/{room_id}", obtained_method="found"):
        return {"success": False, "error": "Inventar voll oder Item nicht uebertragbar"}

    # Aus Raum entfernen
    remove_item_from_room(location_id, room_id, item_id, quantity=quantity)

    item_name = item.get("name", item_id)

    try:
        from app.models.memory import add_memory
        add_memory(character_name,
            f"Ich habe '{item_name}' aufgehoben.",
            tags=["item", "event"])
    except Exception as e:
        logger.debug("Pickup-Memory fehlgeschlagen: %s", e)

    logger.info("%s hat '%s' aus %s/%s aufgehoben", character_name, item_name, location_id, room_id)
    return {"success": True, "item_name": item_name}


def drop_item(character_name: str,
    location_id: str,
    room_id: str,
    item_id: str,
    quantity: int = 1) -> Dict[str, Any]:
    """Character legt ein Item aus dem Inventar in einen Raum ab.

    Schreibt Memory + Diary-Eintrag. Equipped Pieces werden vorher unequipped.

    Returns: {success: bool, error?: str, item_name: str}
    """
    item = get_item(item_id)
    if not item:
        return {"success": False, "error": "Item nicht gefunden"}

    if not has_item(character_name, item_id):
        return {"success": False, "error": "Item nicht im Inventar"}

    # Falls das Piece aktuell angelegt ist: erst ausziehen
    if item.get("category") == "outfit_piece":
        equipped = get_equipped_pieces(character_name)
        for slot, eid in (equipped or {}).items():
            if eid == item_id:
                unequip_piece(character_name, slot=slot)
                break
    else:
        eqi = get_equipped_items(character_name) or []
        if item_id in eqi:
            unequip_item(character_name, item_id)

    # In den Raum legen
    if not add_item_to_room(location_id, room_id, item_id, quantity=quantity):
        return {"success": False, "error": "Konnte Item nicht im Raum ablegen"}

    # Aus Inventar entfernen
    remove_from_inventory(character_name, item_id, quantity=quantity)

    item_name = item.get("name", item_id)

    try:
        from app.models.memory import add_memory
        add_memory(character_name,
            f"Ich habe '{item_name}' abgelegt.",
            tags=["item", "event"])
    except Exception as e:
        logger.debug("Drop-Memory fehlgeschlagen: %s", e)

    logger.info("%s hat '%s' in %s/%s abgelegt", character_name, item_name, location_id, room_id)
    return {"success": True, "item_name": item_name}


# ============================================================
# 4. EQUIPPED-STATE (Outfit-Pieces + sonstige Ausruestung)
# ============================================================

def _get_equipped(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Liefert (equipped_pieces dict, equipped_items list) als Defaults."""
    return {
        "equipped_pieces": dict(profile.get("equipped_pieces") or {}),
        "equipped_items": list(profile.get("equipped_items") or []),
    }


def get_equipped_pieces(character_name: str) -> Dict[str, str]:
    """Slot -> item_id Mapping der angelegten Outfit-Pieces."""
    from app.models.character import get_character_profile
    profile = get_character_profile(character_name)
    return dict(profile.get("equipped_pieces") or {})


def get_equipped_items(character_name: str) -> List[str]:
    """Liste der angelegten Nicht-Piece-Items (Hammer, Brille, etc.)."""
    from app.models.character import get_character_profile
    profile = get_character_profile(character_name)
    return list(profile.get("equipped_items") or [])


def get_equipped_item_ids(character_name: str) -> List[str]:
    """Vereinte Liste aller equipped Item-IDs (Pieces + Items).

    Wird vom Inventar-Filter genutzt um equipped Items auszublenden.
    """
    pieces = get_equipped_pieces(character_name)
    items = get_equipped_items(character_name)
    out = list(pieces.values()) + list(items)
    # Dedupe stable
    seen, dedup = set(), []
    for i in out:
        if i and i not in seen:
            seen.add(i)
            dedup.append(i)
    return dedup


def equip_piece(character_name: str, item_id: str) -> Dict[str, Any]:
    """Legt ein Outfit-Piece an. Belegt alle Slots aus item.outfit_piece.slots
    symmetrisch und verdraengt jedes Piece, das aktuell in einem dieser Slots sitzt.

    Returns: {"status": "ok", "slots": [...], "displaced": [old_item_ids,...]} oder
             {"status": "error", "reason": "..."}
    """
    from app.models.character import get_character_profile, save_character_profile

    if not has_item(character_name, item_id):
        return {"status": "error", "reason": "item_not_in_inventory"}
    item = get_item(item_id)
    if not item:
        return {"status": "error", "reason": "item_not_found"}
    if item.get("category") != "outfit_piece":
        return {"status": "error", "reason": "not_outfit_piece"}
    piece_def = item.get("outfit_piece") or {}
    slots = _clean_piece_slots(piece_def.get("slots"))
    if not slots:
        return {"status": "error", "reason": "invalid_slot"}

    profile = get_character_profile(character_name)
    state = _get_equipped(profile)
    eq = state["equipped_pieces"]

    # Verdraengte Pieces sammeln: jedes Item, das aktuell in einem unserer
    # Ziel-Slots sitzt (und nicht wir selbst sind), wird komplett ausgezogen —
    # inklusive aller seiner Mirror-Slots.
    displaced: List[str] = []
    for s in slots:
        occ = eq.get(s, "")
        if occ and occ != item_id and occ not in displaced:
            displaced.append(occ)

    for displaced_id in displaced:
        for s in list(eq.keys()):
            if eq.get(s) == displaced_id:
                eq.pop(s, None)

    # Falls wir uns selbst woanders gespiegelt haben (z.B. Schema-Drift):
    # alte Eintraege loeschen, dann frisch alle Slots setzen.
    for s in list(eq.keys()):
        if eq.get(s) == item_id and s not in slots:
            eq.pop(s, None)
    for s in slots:
        eq[s] = item_id
    profile["equipped_pieces"] = eq

    # Farb-Meta: alle Slots die durch dieses Equip ein neues Item bekommen
    # haben verlieren ihre Farbe. Eigene unveraenderte Slots behalten sie.
    meta = profile.get("equipped_pieces_meta") or {}
    meta_changed = False
    for s in slots:
        if s in meta:
            meta.pop(s, None)
            meta_changed = True
    if meta_changed:
        profile["equipped_pieces_meta"] = meta
    save_character_profile(character_name, profile)
    logger.info("equip_piece [%s]: slots=%s item=%s%s",
                character_name, slots, item_id,
                f" (verdraengt {displaced})" if displaced else "")
    return {"status": "ok", "slots": slots, "displaced": displaced}


def set_equipped_piece_color(character_name: str, slot: str, color: str) -> Dict[str, Any]:
    """Setzt/loescht die Farbe des aktuell equippten Pieces im Slot.

    Farb-Picker ist ein Roleplay-Element: nur common Rarity kann gefaerbt
    werden. Hoehere Seltenheit hat eine feste, kanonische Farbe.
    Leerer color-String loescht den Meta-Eintrag fuer diesen Slot.
    """
    from app.models.character import get_character_profile, save_character_profile

    if slot not in VALID_PIECE_SLOTS:
        return {"status": "error", "reason": "invalid_slot"}

    profile = get_character_profile(character_name)
    pieces = profile.get("equipped_pieces") or {}
    if not pieces.get(slot):
        return {"status": "error", "reason": "slot_not_equipped"}

    # Rarity-Gate: nur "generic" Outfits sind einfaerbbar — alle anderen
    # haben ihre Farbe fest im Prompt verankert.
    _it = get_item(pieces.get(slot)) or {}
    _rarity = (_it.get("rarity") or "common").lower()
    if _rarity != "generic" and color:
        return {"status": "error", "reason": "rarity_not_colorable"}

    meta = profile.get("equipped_pieces_meta") or {}
    color = (color or "").strip()
    if color:
        slot_meta = meta.get(slot) or {}
        if not isinstance(slot_meta, dict):
            slot_meta = {}
        slot_meta["color"] = color
        meta[slot] = slot_meta
    else:
        meta.pop(slot, None)
    profile["equipped_pieces_meta"] = meta
    save_character_profile(character_name, profile)
    logger.info("set_equipped_piece_color [%s]: slot=%s color=%r",
                character_name, slot, color or "(clear)")
    return {"status": "ok", "slot": slot, "color": color, "item_id": pieces.get(slot)}


def unequip_piece(character_name: str,
                   slot: str = "", item_id: str = "") -> Dict[str, Any]:
    """Entfernt ein Piece — entweder per slot oder per item_id.

    Multi-Slot-Pieces werden vollstaendig ausgezogen: alle Slots in denen
    das Item sitzt werden geleert. Das Piece bleibt im Inventar, ist nur
    nicht mehr equipped. Farb-Meta fuer alle betroffenen Slots wird entfernt.
    """
    from app.models.character import get_character_profile, save_character_profile
    profile = get_character_profile(character_name)
    state = _get_equipped(profile)
    eq = state["equipped_pieces"]

    target_slot = ""
    if slot and slot in eq:
        target_slot = slot
    elif item_id:
        for s, iid in eq.items():
            if iid == item_id:
                target_slot = s
                break
    if not target_slot:
        return {"status": "error", "reason": "not_equipped"}

    removed = eq[target_slot]
    cleared_slots = [s for s, iid in list(eq.items()) if iid == removed]
    for s in cleared_slots:
        eq.pop(s, None)
    profile["equipped_pieces"] = eq
    meta = profile.get("equipped_pieces_meta") or {}
    for s in cleared_slots:
        meta.pop(s, None)
    profile["equipped_pieces_meta"] = meta
    save_character_profile(character_name, profile)
    logger.info("unequip_piece [%s]: item=%s slots=%s",
                character_name, removed, cleared_slots)
    return {"status": "ok", "slot": target_slot, "item_id": removed,
            "cleared_slots": cleared_slots}


def equip_item(character_name: str, item_id: str) -> Dict[str, Any]:
    """Legt ein Nicht-Piece-Item an (z.B. Hammer, Brille). Item muss im
    Inventar sein. Outfit-Pieces gehen ueber equip_piece, nicht hier.
    """
    from app.models.character import get_character_profile, save_character_profile
    if not has_item(character_name, item_id):
        return {"status": "error", "reason": "item_not_in_inventory"}
    item = get_item(item_id)
    if not item:
        return {"status": "error", "reason": "item_not_found"}
    if item.get("category") == "outfit_piece":
        return {"status": "error", "reason": "use_equip_piece_for_outfit_piece"}

    profile = get_character_profile(character_name)
    state = _get_equipped(profile)
    items = state["equipped_items"]
    if item_id in items:
        return {"status": "ok", "already_equipped": True}
    items.append(item_id)
    profile["equipped_items"] = items
    save_character_profile(character_name, profile)
    logger.info("equip_item [%s]: %s", character_name, item_id)
    return {"status": "ok"}


def unequip_item(character_name: str, item_id: str) -> Dict[str, Any]:
    """Entfernt ein Nicht-Piece-Item aus der Ausruestung. Bleibt im Inventar."""
    from app.models.character import get_character_profile, save_character_profile
    profile = get_character_profile(character_name)
    state = _get_equipped(profile)
    items = state["equipped_items"]
    if item_id not in items:
        return {"status": "error", "reason": "not_equipped"}
    items.remove(item_id)
    profile["equipped_items"] = items
    save_character_profile(character_name, profile)
    logger.info("unequip_item [%s]: %s", character_name, item_id)
    return {"status": "ok"}


# ============================================================
# 4b. ZENTRALER EQUIP-DIFF
# ============================================================

def apply_equipped_pieces(character_name: str, *,
    pieces: Optional[Dict[str, str]] = None,
    items: Optional[List[str]] = None,
    remove_slots: Optional[List[str]] = None,
    pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    source: str = "") -> Dict[str, Any]:
    """Atomarer Equip-Wechsel mit Diff-Erkennung.

    pieces: Soll-Zustand der Piece-Slots (slot -> item_id). None = Pieces bleiben
            unveraendert. Dict uebergeben (auch leer) = vollstaendiger Soll-State;
            Slots die belegt sind aber weder in `pieces` noch in `remove_slots`
            auftauchen, werden geleert.
    items:  Soll-Zustand der Nicht-Piece-Items (Liste). None = unveraendert.
    remove_slots: Slots die explizit geleert werden (auch ohne pieces-Argument).
    pieces_meta: Optionale Per-Slot-Metadaten (z.B. {"outer": {"color": "red"}}).
            Wird in equipped_pieces_meta gemerged und ueberschreibt die
            Auto-Cleanup-Logik (d.h. wer Meta mitliefert, bestimmt endgueltig).
    source: Freitext zum Logging (ui_wardrobe, telegram, skill, ...).

    Returns: {
        "status": "ok",
        "changed": bool,
        "applied": [{item_id, slot}],
        "cleared": [{slot, item_id}],
        "skipped": [{item_id, reason, slot?}],
        "pieces_before": {...}, "pieces_after": {...},
        "items_before": [...], "items_after": [...],
    }
    """
    from app.models.character import get_character_profile, save_character_profile
    profile = get_character_profile(character_name)

    before_pieces = dict(profile.get("equipped_pieces") or {})
    before_items = list(profile.get("equipped_items") or [])

    target_pieces = dict(before_pieces)
    target_items = list(before_items)

    applied: List[Dict[str, Any]] = []
    cleared: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    rs = list(remove_slots or [])
    # remove_slots: das gesamte Item aus dem Slot entfernen (inkl. Mirror-Slots).
    for slot in rs:
        iid = target_pieces.get(slot)
        if not iid:
            continue
        for s in [s for s, x in list(target_pieces.items()) if x == iid]:
            cleared.append({"slot": s, "item_id": iid})
            target_pieces.pop(s, None)

    if pieces is not None:
        # 1) Pieces, die nicht mehr im Soll-State sind, raeumen wir vollstaendig
        #    weg — inklusive aller Mirror-Slots des betroffenen Items.
        wanted_ids = {iid for iid in pieces.values() if iid}
        for slot in list(target_pieces.keys()):
            iid = target_pieces.get(slot)
            if not iid:
                continue
            if iid in wanted_ids:
                continue  # Item bleibt, evtl. mit anderen Slots — gleich neu setzen
            if slot in pieces or slot in rs:
                # Slot wird gleich ueberschrieben/geleert — Cleanup uebernimmt das
                continue
            for s in [s for s, x in list(target_pieces.items()) if x == iid]:
                cleared.append({"slot": s, "item_id": iid})
                target_pieces.pop(s, None)

        # 2) Pro Item dedup, dann Multi-Slot-Pieces ZUERST verarbeiten —
        #    sonst kann ein Single-Slot-Piece einen Mirror-Slot eines Multi-Slot-
        #    Pieces "stehlen" und das Multi-Slot-Piece bricht auseinander.
        item_entries: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for slot, iid in pieces.items():
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)
            if not has_item(character_name, iid):
                skipped.append({"item_id": iid, "slot": slot, "reason": "not_in_inventory"})
                continue
            it = get_item(iid)
            if not it or it.get("category") != "outfit_piece":
                skipped.append({"item_id": iid, "slot": slot, "reason": "not_outfit_piece"})
                continue
            item_slots_list = _clean_piece_slots((it.get("outfit_piece") or {}).get("slots"))
            if not item_slots_list:
                skipped.append({"item_id": iid, "slot": slot, "reason": "invalid_slot"})
                continue
            item_entries.append({"iid": iid, "input_slot": slot, "slots": item_slots_list})

        # Multi-Slot zuerst, dann Single-Slot (stable bei gleicher slot-count
        # ueber input-Reihenfolge).
        item_entries.sort(key=lambda e: -len(e["slots"]))

        locked: Dict[str, str] = {}  # slot -> piece_id (gewinnender Anspruch)
        for e in item_entries:
            iid = e["iid"]
            item_slots_list = e["slots"]
            blocked = next(
                (ts for ts in item_slots_list
                 if ts in locked and locked[ts] != iid),
                None)
            if blocked is not None:
                skipped.append({"item_id": iid, "slot": e["input_slot"],
                                "reason": "slot_conflict_with_multi_slot"})
                continue
            # Verdraengen: bestehende Items, die in einem Ziel-Slot sitzen,
            # komplett ausziehen (auch ihre Mirror-Slots).
            for ts in item_slots_list:
                occ = target_pieces.get(ts)
                if occ and occ != iid:
                    for s in [s for s, x in list(target_pieces.items()) if x == occ]:
                        cleared.append({"slot": s, "item_id": occ})
                        target_pieces.pop(s, None)
            # Eigene Stale-Mirrors raeumen (Schema-Drift), dann frisch setzen.
            for s in [s for s, x in list(target_pieces.items()) if x == iid and s not in item_slots_list]:
                target_pieces.pop(s, None)
            for ts in item_slots_list:
                target_pieces[ts] = iid
                locked[ts] = iid
                applied.append({"item_id": iid, "slot": ts})

    if items is not None:
        validated: List[str] = []
        seen = set()
        for iid in items:
            if not iid or iid in seen:
                continue
            if not has_item(character_name, iid):
                skipped.append({"item_id": iid, "reason": "not_in_inventory"})
                continue
            seen.add(iid)
            validated.append(iid)
        target_items = validated

    changed = (target_pieces != before_pieces) or (target_items != before_items)

    # Meta-Handling: zuerst Auto-Cleanup (Slots wo Item wechselt/leer wird),
    # dann Overrides aus pieces_meta anwenden.
    meta = dict(profile.get("equipped_pieces_meta") or {})
    incoming_meta_slots = set((pieces_meta or {}).keys())
    meta_changed = False
    if changed:
        for _slot in list(meta.keys()):
            if _slot in incoming_meta_slots:
                continue  # Explizit gesetzter Slot — nicht automatisch raeumen
            _new_iid = target_pieces.get(_slot)
            _old_iid = before_pieces.get(_slot)
            if not _new_iid or _new_iid != _old_iid:
                meta.pop(_slot, None)
                meta_changed = True

    if pieces_meta:
        for _slot, _slot_meta in pieces_meta.items():
            if not isinstance(_slot_meta, dict):
                continue
            color = (_slot_meta.get("color") or "").strip()
            if not target_pieces.get(_slot):
                # Kein Piece im Slot -> keine Meta sinnvoll, evtl. bestehende entfernen
                if _slot in meta:
                    meta.pop(_slot, None)
                    meta_changed = True
                continue
            # Rarity-Gate: nur "generic" Outfits nehmen Farben an
            _it = get_item(target_pieces[_slot]) or {}
            _rarity = (_it.get("rarity") or "common").lower()
            if _rarity != "generic" and color:
                if _slot in meta:
                    meta.pop(_slot, None)
                    meta_changed = True
                continue
            current = meta.get(_slot) or {}
            new_entry = dict(current)
            if color:
                new_entry["color"] = color
            else:
                new_entry.pop("color", None)
            if new_entry:
                if meta.get(_slot) != new_entry:
                    meta[_slot] = new_entry
                    meta_changed = True
            elif _slot in meta:
                meta.pop(_slot, None)
                meta_changed = True

    if changed or meta_changed:
        profile["equipped_pieces"] = target_pieces
        profile["equipped_items"] = target_items
        profile["equipped_pieces_meta"] = meta
        save_character_profile(character_name, profile)
        logger.info(
            "apply_equipped_pieces [%s] source=%s pieces=%d items=%d changed=1",
            character_name, source or "?", len(target_pieces), len(target_items))
        try:
            from app.core.outfit_events import publish as _publish_outfit
            _publish_outfit(character_name, source)
        except Exception as _pe:
            logger.debug("outfit-event publish fehlgeschlagen: %s", _pe)

    return {
        "status": "ok",
        "changed": changed or meta_changed,
        "applied": applied,
        "cleared": cleared,
        "skipped": skipped,
        "pieces_before": before_pieces,
        "pieces_after": target_pieces,
        "items_before": before_items,
        "items_after": target_items,
        "meta_after": meta,
    }


# ============================================================
# 5. OUTFIT-TYPE COMPLIANCE (Auto-Swap bei Location/Raum-Wechsel)
# ============================================================

def _piece_matches_type(item: Dict[str, Any], target_type: str) -> bool:
    """Prueft ob ein Piece-Item zum Outfit-Type passt.

    - Kein target_type -> alle passen
    - Piece hat keine outfit_types -> neutral (passt immer)
    - Sonst: target_type muss in outfit_types enthalten sein (case-insensitive)
    """
    if not target_type:
        return True
    op = item.get("outfit_piece") or {}
    types = [t.strip().lower() for t in (op.get("outfit_types") or []) if t]
    if not types:
        return True  # Neutral — keine Einschraenkung
    return target_type.strip().lower() in types


def _piece_is_strict_type(item: Dict[str, Any], target_type: str) -> bool:
    """True nur wenn das Piece explizit target_type in outfit_types hat
    (ohne neutralen Pass-Fallback)."""
    if not target_type:
        return False
    op = item.get("outfit_piece") or {}
    types = [t.strip().lower() for t in (op.get("outfit_types") or []) if t]
    return target_type.strip().lower() in types


def apply_outfit_type_compliance(character_name: str,
                                 target_type: str) -> Dict[str, Any]:
    """Prueft angelegte Pieces gegen target_type und tauscht nicht-passende
    gegen Inventar-Pieces gleichen Slots mit passendem Typ.

    Returns Dict mit 'swapped' und 'removed' Listen fuer Logging.
    """
    if not target_type:
        return {"status": "ok", "swapped": [], "removed": []}

    # Permissive Types (required-Liste leer) erzwingen keine Kleidung —
    # z.B. ``bed`` und ``bath``: Char darf nackt sein, in Lingerie, im
    # Bademantel, alles ist OK. Compliance darf hier nicht reingrätschen
    # und Pieces austauschen oder als "passt nicht"-mismatch markieren.
    # Ohne diesen Skip wuerde ein Char in Adventure-Klamotten die Lirien
    # gerade trägt waehrend "sleeping" als komplette Mismatch-Liste
    # auftauchen, obwohl der bed-Type per Definition keine Vorschriften
    # macht. Ist der Type aber strict (required > 0): normale Compliance
    # laeuft wie bisher.
    try:
        from app.core.outfit_rules import baseline_required_slots
        if not baseline_required_slots(target_type):
            return {"status": "ok",
                    "swapped": [], "removed": [], "filled": [],
                    "skipped": "permissive_type"}
    except Exception:
        pass

    from app.models.character import get_character_profile
    profile = get_character_profile(character_name)
    eq_pieces = dict(profile.get("equipped_pieces") or {})

    # Auch wenn der Character nackt ist (eq_pieces leer): wir laufen weiter,
    # damit auto_fill_missing_slots am Ende greift und ein passendes Outfit
    # aus dem Inventar zusammenstellt. Frueher gab es hier einen early-return,
    # der dazu fuehrte dass nackte Characters in business/formal-Locations
    # nackt blieben.

    # Inventory des Characters + Item-Definitionen
    inv = _load_inventory(character_name).get("inventory", [])

    swapped, removed = [], []
    # Pieces die nicht passen aber kein Ersatz im Inventar verfuegbar — am
    # Ende einmal gesammelt geloggt + notified statt pro Piece einen Eintrag
    # zu fluten (vorher: 4 Pieces = 4 Warnings + 4 Notifications fuer einen
    # Compliance-Run).
    mismatched: List[Dict[str, Any]] = []

    # Hilfsfunktion: sucht einen strikt/neutral passenden Ersatz, der ALLE
    # angegebenen Slots abdeckt. Multi-Slot-Pieces matchen wenn ihre slots
    # genau die required_slots enthalten (Set-Vergleich).
    def _find_replacement(required_slots: List[str], already_equipped: set) -> Optional[str]:
        req = set(required_slots)
        if not req:
            return None
        strict = None
        neutral = None
        for e in inv:
            cand_id = e.get("item_id")
            if not cand_id or cand_id in already_equipped:
                continue
            cand = get_item(cand_id)
            if not cand or cand.get("category") != "outfit_piece":
                continue
            cand_slots = set(_piece_slots(cand))
            # Wir suchen ein Piece das genau dieselbe Slot-Kombination belegt
            # — nicht mehr und nicht weniger. Sonst wuerde z.B. ein Kleid
            # (top+bottom) einen einzelnen top-Slot ersetzen und dabei
            # bottom verdraengen.
            if cand_slots != req:
                continue
            if _piece_is_strict_type(cand, target_type):
                strict = cand_id
                break  # bester Treffer
            if neutral is None and _piece_matches_type(cand, target_type):
                neutral = cand_id
        return strict or neutral

    # Eq nach Pieces gruppieren (jedes Piece kann mehrere Slots belegen).
    # Wir iterieren ueber distinct items in stabiler Reihenfolge — VALID_PIECE_SLOTS
    # gibt die Insertion-Order.
    seen_pieces: List[tuple] = []  # (iid, [slots,...]) in Reihenfolge
    by_iid: Dict[str, List[str]] = {}
    for s in VALID_PIECE_SLOTS:
        iid = eq_pieces.get(s)
        if not iid:
            continue
        if iid not in by_iid:
            by_iid[iid] = []
            seen_pieces.append((iid, by_iid[iid]))
        by_iid[iid].append(s)

    for iid, piece_slots in seen_pieces:
        cur_item = get_item(iid)
        if not cur_item:
            # Stale-Item — ausziehen
            for s in piece_slots:
                eq_pieces.pop(s, None)
            removed.append({"slots": list(piece_slots), "old": iid})
            continue
        cur_strict = _piece_is_strict_type(cur_item, target_type)
        cur_matches = _piece_matches_type(cur_item, target_type)

        if cur_strict:
            continue  # passt strikt — fertig

        # Bereits in Verwendung markieren (sich selbst nicht ersetzen)
        already = {x for x in eq_pieces.values() if x}

        if cur_matches:
            # Neutral — nur tauschen wenn ein strikter Ersatz existiert
            for e in inv:
                cand_id = e.get("item_id")
                if not cand_id or cand_id in already:
                    continue
                cand = get_item(cand_id)
                if not cand or cand.get("category") != "outfit_piece":
                    continue
                if set(_piece_slots(cand)) != set(piece_slots):
                    continue
                if _piece_is_strict_type(cand, target_type):
                    for s in piece_slots:
                        eq_pieces[s] = cand_id
                    swapped.append({"slots": list(piece_slots), "old": iid, "new": cand_id})
                    break
            continue

        # Passt nicht — strikter oder neutraler Ersatz
        replacement_id = _find_replacement(piece_slots, already)
        if replacement_id:
            for s in piece_slots:
                eq_pieces[s] = replacement_id
            swapped.append({"slots": list(piece_slots), "old": iid, "new": replacement_id})
        else:
            # Kein Ersatz im Inventar — Piece BLEIBT angezogen statt den
            # Character nackt zu lassen. Frueher wurde hier .pop() gemacht
            # was zur Folge hatte, dass eine Char ohne passende Garderobe
            # an einem strikten Ort komplett ausgekleidet wurde und auch
            # bleiben musste (compliance feuert nur bei Loc/Activity-Change).
            # Sammeln und am Ende EINMAL melden statt pro Piece zu spammen.
            mismatched.append({
                "name": cur_item.get("name") or iid,
                "slots": list(piece_slots),
            })

    changed = bool(swapped or removed)
    if changed:
        profile["equipped_pieces"] = eq_pieces
        from app.models.character import save_character_profile
        save_character_profile(character_name, profile)
        logger.info("outfit_type-Compliance [%s] target=%s: %d getauscht, %d entfernt",
                    character_name, target_type, len(swapped), len(removed))

    # Aggregierte Mismatch-Meldung — eine Warning + eine Notification pro
    # Compliance-Run statt pro Piece. Spart Logs/Notifications wenn ein
    # Char z.B. mit 4 Adventure-Pieces in einem bed-Location landet.
    if mismatched:
        _names = ", ".join(f"'{m['name']}'" for m in mismatched)
        logger.warning(
            "outfit_type-Mismatch [%s] target=%s: %d Piece(s) ohne Ersatz im "
            "Inventar — bleiben angezogen: %s",
            character_name, target_type, len(mismatched), _names)
        try:
            from app.models.notifications import add_notification
            add_notification(
                character_name,
                (f"⚠️ {len(mismatched)} Stueck(e) passen nicht zum "
                 f"{target_type}-Dresscode: {_names}. Kein passender Ersatz "
                 "im Inventar — Outfit bleibt unveraendert."),
                notification_type="outfit_mismatch")
        except Exception:
            pass

    # Auto-Fill: fehlende required_slots aus Inventar auffuellen
    filled = auto_fill_missing_slots(character_name, target_type)
    if changed or filled:
        try:
            from app.core.outfit_events import publish as _publish_outfit
            _publish_outfit(character_name, "location_compliance")
        except Exception as _pe:
            logger.debug("outfit-event publish (compliance) fehlgeschlagen: %s", _pe)
    return {"status": "ok", "changed": changed or bool(filled),
            "swapped": swapped, "removed": removed, "filled": filled}


def auto_fill_missing_slots(character_name: str, outfit_type: str) -> List[Dict[str, str]]:
    """Fuellt fehlende required_slots aus dem Inventar des Characters auf.

    Nutzt outfit_rules.resolve_required_slots + Character-Exceptions.
    Liefert Liste der gefuellten Slots [{slot, item_id}].
    """
    if not outfit_type or not character_name:
        return []
    try:
        from app.core.outfit_rules import resolve_required_slots
    except Exception:
        return []

    from app.models.character import get_character_profile, save_character_profile

    profile = get_character_profile(character_name) or {}
    eq_pieces = dict(profile.get("equipped_pieces") or {})
    runtime_skip = list(profile.get("runtime_outfit_skip") or [])
    required = resolve_required_slots(
        outfit_type, character_name,
        runtime_force_skip=runtime_skip,
    )
    # Gecoverte Slots zaehlen als "bereits erledigt" — z.B. Kleid in top mit
    # covers=[bottom] deckt bottom ab, kein separates bottom-Piece noetig.
    from app.models.character import _collect_covered_slots
    covered = _collect_covered_slots(eq_pieces)
    missing = [s for s in required
               if not eq_pieces.get(s) and s not in covered]
    if not missing:
        return []

    inv = _load_inventory(character_name).get("inventory", [])
    used_ids = set(eq_pieces.values())
    filled: List[Dict[str, str]] = []

    def _pick_piece(slot: str) -> Optional[str]:
        """Bevorzugt strict-typed, sonst neutral-typed piece fuer den Slot.

        Bei mehreren gleichwertigen Kandidaten waehlt ein Tages-Seed
        (character+date+slot) deterministisch — gleiche Auswahl ueber den
        Tag, naechster Tag potenziell anderes Outfit.
        """
        strict: List[str] = []
        neutral: List[str] = []
        for e in inv:
            iid = e.get("item_id")
            if not iid or iid in used_ids:
                continue
            it = get_item(iid)
            if not it or it.get("category") != "outfit_piece":
                continue
            if slot not in _piece_slots(it):
                continue
            if _piece_is_strict_type(it, outfit_type):
                strict.append(iid)
            elif _piece_matches_type(it, outfit_type):
                neutral.append(iid)
        pool = strict or neutral
        if not pool:
            return None
        rng = random.Random(f"{character_name}|{date.today().isoformat()}|{slot}")
        return rng.choice(pool)

    for slot in missing:
        # Ein Multi-Slot-Pick aus einer frueheren Iteration kann diesen Slot
        # bereits gefuellt haben (z.B. Kleid deckt top + bottom).
        if eq_pieces.get(slot):
            continue
        pick = _pick_piece(slot)
        if not pick:
            continue

        # Alle Slots des gewaehlten Pieces symmetrisch belegen. Bestehende
        # Pieces in diesen Slots werden vollstaendig verdraengt — sonst
        # blockiert z.B. ein bereits angezogener Rock das Auto-Anziehen
        # eines Kleides, das top+bottom belegt.
        it = get_item(pick)
        item_slots = _piece_slots(it)
        if slot not in item_slots:
            continue  # Defensive — sollte durch _pick_piece nicht vorkommen
        displaced: List[str] = []
        for s in item_slots:
            occ = eq_pieces.get(s) or ""
            if occ and occ != pick and occ not in displaced:
                displaced.append(occ)
        for displaced_id in displaced:
            for s in list(eq_pieces.keys()):
                if eq_pieces.get(s) == displaced_id:
                    eq_pieces.pop(s, None)
            used_ids.discard(displaced_id)

        for s in item_slots:
            eq_pieces[s] = pick
        used_ids.add(pick)
        filled.append({"slot": slot, "item_id": pick, "slots": list(item_slots)})
        if displaced:
            logger.info("auto_fill [%s]: %s belegt slots %s — verdraengt %s",
                        character_name, pick, item_slots, displaced)

    if filled:
        profile["equipped_pieces"] = eq_pieces
        save_character_profile(character_name, profile)
        logger.info("auto_fill [%s] %s: %d Slots gefuellt: %s",
                    character_name, outfit_type, len(filled),
                    ", ".join(f"{f['slot']}={f['item_id']}" for f in filled))
    return filled

"""Story Arcs — persistente Multi-Character Storylines.

Speichert Story Arcs als JSON pro User:
    storage/users/{user_id}/story_arcs.json
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("story_arcs")


def _arcs_path() -> Path:
    from app.core.paths import get_storage_dir
    return get_storage_dir() / "story_arcs.json"


def _row_to_arc(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile in ein Arc-Dict.
    Schema: (id, title, state, beats, participants, meta, created_at, updated_at)
    """
    meta = {}
    try:
        meta = json.loads(row[5] or "{}")
    except Exception:
        pass
    if meta and "id" in meta:
        return meta
    # Minimal fallback
    arc = {
        "id": row[0],
        "title": row[1] or "",
        "status": row[2] or "active",
        "created_at": row[6] or "",
        "updated_at": row[7] or "",
    }
    try:
        arc["beats"] = json.loads(row[3] or "[]")
    except Exception:
        arc["beats"] = []
    try:
        arc["participants"] = json.loads(row[4] or "[]")
    except Exception:
        arc["participants"] = []
    arc.update(meta)
    return arc


def _load() -> Dict[str, Any]:
    """Laedt alle Story Arcs aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, title, state, beats, participants, meta, created_at, updated_at "
            "FROM story_arcs ORDER BY created_at ASC"
        ).fetchall()
        arcs = [_row_to_arc(r) for r in rows]
        # last_generated: neuester updated_at-Timestamp unter aktiven Arcs
        last_gen = ""
        active = [a for a in arcs if a.get("status") == "active"]
        if active:
            last_gen = max((a.get("updated_at", "") for a in active), default="")
        return {"arcs": arcs, "last_generated": last_gen, "generation_cooldown_hours": 6}
    except Exception as e:
        logger.warning("_load story_arcs DB-Fehler: %s", e)
        # Fallback: JSON-Datei
        p = _arcs_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Fehler beim Laden: %s", exc)
        return {"arcs": [], "last_generated": "", "generation_cooldown_hours": 6}


def _save(data: Dict[str, Any]) -> None:
    """Speichert alle Story Arcs in die DB (Upsert)."""
    arcs = data.get("arcs", [])
    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM story_arcs"
            ).fetchall()}
            new_ids = {a.get("id") for a in arcs if a.get("id")}

            for arc_id in existing_ids - new_ids:
                conn.execute("DELETE FROM story_arcs WHERE id=?", (arc_id,))

            for arc in arcs:
                arc_id = arc.get("id")
                if not arc_id:
                    continue
                conn.execute("""
                    INSERT INTO story_arcs
                        (id, title, state, beats, participants, meta, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        state=excluded.state,
                        beats=excluded.beats,
                        participants=excluded.participants,
                        meta=excluded.meta,
                        updated_at=excluded.updated_at
                """, (
                    arc_id,
                    arc.get("title", ""),
                    arc.get("status", "active"),
                    json.dumps(arc.get("beats", []), ensure_ascii=False),
                    json.dumps(arc.get("participants", []), ensure_ascii=False),
                    json.dumps(arc, ensure_ascii=False),
                    arc.get("created_at", now),
                    arc.get("updated_at", now),
                ))
    except Exception as e:
        logger.error("_save story_arcs DB-Fehler: %s", e)


def get_active_arcs(character_name: str = "") -> List[Dict[str, Any]]:
    """Gibt aktive Arcs zurueck, optional gefiltert nach Character."""
    data = _load()
    arcs = [a for a in data["arcs"] if a.get("status") == "active"]
    if character_name:
        arcs = [a for a in arcs if character_name in a.get("participants", [])]
    return arcs


def get_all_arcs() -> List[Dict[str, Any]]:
    """Gibt alle Arcs zurueck (fuer Status-Endpoint)."""
    return _load().get("arcs", [])


def get_arc(arc_id: str) -> Optional[Dict[str, Any]]:
    """Gibt einen einzelnen Arc zurueck."""
    data = _load()
    for arc in data["arcs"]:
        if arc["id"] == arc_id:
            return arc
    return None


def create_arc(title: str,
    participants: List[str],
    seed: str,
    tension: int = 1,
    first_beat_hint: str = "",
    max_beats: int = 5) -> Dict[str, Any]:
    """Erstellt einen neuen Story Arc."""
    now = datetime.now().isoformat()
    arc = {
        "id": f"arc_{uuid.uuid4().hex[:8]}",
        "title": title,
        "status": "active",
        "participants": participants,
        "seed": seed,
        "current_state": seed,
        "tension": max(1, min(5, tension)),
        "beats": [],
        "max_beats": max_beats,
        "next_beat_hint": first_beat_hint,
        "created_at": now,
        "updated_at": now,
    }
    data = _load()
    data["arcs"].append(arc)
    data["last_generated"] = now
    _save(data)
    logger.info("Neuer Arc: '%s' mit %s", title, participants)
    return arc


def advance_arc(arc_id: str,
    beat_summary: str,
    new_state: str,
    tension: int,
    next_beat_hint: str = "",
    resolved: bool = False) -> Optional[Dict[str, Any]]:
    """Fuegt einen neuen Beat hinzu und aktualisiert den Arc."""
    data = _load()
    for arc in data["arcs"]:
        if arc["id"] != arc_id:
            continue
        now = datetime.now().isoformat()
        beat_num = len(arc["beats"]) + 1
        arc["beats"].append({
            "beat": beat_num,
            "timestamp": now,
            "summary": beat_summary,
        })
        arc["current_state"] = new_state
        arc["tension"] = max(1, min(5, tension))
        arc["next_beat_hint"] = next_beat_hint
        arc["updated_at"] = now
        if resolved or beat_num >= arc["max_beats"]:
            arc["status"] = "resolved"
            logger.info("Arc resolved: '%s' nach %d Beats", arc["title"], beat_num)
        else:
            logger.info("Arc advanced: '%s' Beat %d/%d (tension=%d)",
                        arc["title"], beat_num, arc["max_beats"], tension)
        _save(data)
        return arc
    logger.warning("Arc %s nicht gefunden", arc_id)
    return None


def resolve_arc(arc_id: str,
    resolution: str,
    character_outcomes: Dict[str, str] = None,
    sequel_seed: str = "") -> Optional[Dict[str, Any]]:
    """Schliesst einen Arc ab mit Resolution-Text."""
    data = _load()
    for arc in data["arcs"]:
        if arc["id"] != arc_id:
            continue
        now = datetime.now().isoformat()
        arc["status"] = "resolved"
        arc["updated_at"] = now
        arc["resolution"] = resolution
        if character_outcomes:
            arc["character_outcomes"] = character_outcomes
        if sequel_seed:
            arc["sequel_seed"] = sequel_seed
        _save(data)
        logger.info("Arc abgeschlossen: '%s'", arc["title"])
        return arc
    return None


def can_generate(max_active: int = 2, cooldown_hours: float = 6) -> bool:
    """Prueft ob ein neuer Arc generiert werden darf."""
    data = _load()
    active = [a for a in data["arcs"] if a.get("status") == "active"]
    if len(active) >= max_active:
        return False
    last = data.get("last_generated", "")
    if last:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
            if elapsed < cooldown_hours:
                return False
        except (ValueError, TypeError):
            pass
    return True


def attach_beat_image(arc_id: str,
    beat_num: int,
    image_info: Dict[str, Any]) -> bool:
    """Haengt Image-Metadaten an einen Beat an."""
    data = _load()
    for arc in data["arcs"]:
        if arc["id"] != arc_id:
            continue
        for beat in arc.get("beats", []):
            if beat.get("beat") == beat_num:
                beat["scene_image"] = image_info
                _save(data)
                return True
    return False


def remove_arc(arc_id: str) -> bool:
    """Entfernt einen einzelnen Arc anhand seiner ID."""
    data = _load()
    before = len(data["arcs"])
    data["arcs"] = [a for a in data["arcs"] if a["id"] != arc_id]
    if len(data["arcs"]) == before:
        return False
    _save(data)
    logger.info("Arc gelöscht: %s", arc_id)
    return True


def cleanup_old_arcs(max_resolved: int = 20) -> int:
    """Entfernt alte resolved Arcs ueber dem Limit."""
    data = _load()
    resolved = [a for a in data["arcs"] if a.get("status") == "resolved"]
    if len(resolved) <= max_resolved:
        return 0
    # Aelteste zuerst entfernen
    resolved.sort(key=lambda a: a.get("updated_at", ""))
    to_remove = set()
    for arc in resolved[:len(resolved) - max_resolved]:
        to_remove.add(arc["id"])
    data["arcs"] = [a for a in data["arcs"] if a["id"] not in to_remove]
    _save(data)
    return len(to_remove)

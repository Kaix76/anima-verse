"""Assignment-System — Temporaere, priorisierte Aufgaben fuer Characters.

Assignments sind zeitlich begrenzte Aufgaben, die einem oder mehreren
Characters zugeordnet werden koennen. Im Gegensatz zu `character_task`
(permanente Rolle) haben Assignments eine Frist und Prioritaet.

Storage: storage/users/{username}/assignments.json (User-Level)
"""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("assignments")


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _get_assignments_path() -> Path:
    """Returns path to the user's assignments.json file."""
    from app.core.paths import get_storage_dir
    return get_storage_dir() / "assignments.json"


def _load_all() -> List[Dict[str, Any]]:
    """Load all assignments from the DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, character_name, task, status, due, meta, created_at, updated_at "
            "FROM assignments ORDER BY created_at ASC"
        ).fetchall()
        assignments = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[5] or "{}")
            except Exception:
                pass
            if meta and "id" in meta:
                assignments.append(meta)
            else:
                # Minimal fallback
                assignments.append({
                    "id": r[0],
                    "title": r[2] or "",
                    "description": "",
                    "status": r[3] or "active",
                    "expires_at": r[4] or None,
                    "created_at": r[6] or "",
                    "participants": {},
                    "priority": 3,
                    "target_count": 0,
                    "source": "db",
                })
        return assignments
    except Exception as e:
        logger.warning("_load_all assignments DB-Fehler: %s", e)
        # Fallback: JSON-Datei
        path = _get_assignments_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error("Failed to load assignments: %s", exc)
            return []


def _save_all(assignments: List[Dict[str, Any]]) -> None:
    """Save all assignments to DB (upsert) and JSON backup."""
    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM assignments"
            ).fetchall()}
            new_ids = {a.get("id") for a in assignments if a.get("id")}

            for aid in existing_ids - new_ids:
                conn.execute("DELETE FROM assignments WHERE id=?", (aid,))

            for a in assignments:
                aid = a.get("id")
                if not aid:
                    continue
                conn.execute("""
                    INSERT INTO assignments
                        (id, character_name, task, status, due, meta, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        character_name=excluded.character_name,
                        task=excluded.task,
                        status=excluded.status,
                        due=excluded.due,
                        meta=excluded.meta,
                        updated_at=excluded.updated_at
                """, (
                    aid,
                    next(iter(a.get("participants", {}).keys()), None),
                    a.get("title", a.get("task", "")),
                    a.get("status", "active"),
                    a.get("expires_at", ""),
                    json.dumps(a, ensure_ascii=False),
                    a.get("created_at", now),
                    now,
                ))
    except Exception as e:
        logger.error("_save_all assignments DB-Fehler: %s", e)

    # JSON-Backup
    try:
        path = _get_assignments_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(assignments, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

PRIORITY_LABELS = {
    1: "DRINGEND",
    2: "HOCH",
    3: "NORMAL",
    4: "NIEDRIG",
    5: "HINTERGRUND",
}


def _extract_target_count(text: str) -> int:
    """Extract a numeric target from text like '5 Fotos', '20 Beispiele', '10 Rezepte'.

    Recognizes photo, search/research, and general countable task keywords.
    Returns 0 if no target found.
    """
    import re
    _KEYWORDS = (
        # Images
        r'Fotos?|fotos?|Photos?|photos?|Bilder?|bilder?|Images?|images?'
        r'|Aufnahmen?|aufnahmen?|Shots?|shots?|Pics?|pics?'
        # Search / Research
        r'|Beispiele?|beispiele?|Ergebnisse?|ergebnisse?|Quellen?|quellen?'
        r'|Links?|links?|Artikel?|artikel?|Rezepte?|rezepte?'
        r'|Tipps?|tipps?|Ideen?|ideen?|Vorschl[aä]ge?|vorschl[aä]ge?'
        r'|Eintr[aä]ge?|eintr[aä]ge?|Results?|results?|Examples?|examples?'
        # General countable
        r'|Posts?|posts?|Nachrichten?|nachrichten?|Kommentare?|kommentare?'
    )
    m = re.search(rf'(\d+)\s*(?:{_KEYWORDS})', text)
    if m:
        return int(m.group(1))
    return 0


def create_assignment(title: str,
    description: str,
    participants: Dict[str, Dict[str, Any]],
    priority: int = 3,
    duration_minutes: Optional[int] = None,
    expires_at: Optional[str] = None,
    location_id: Optional[str] = None,
    outfit_hint: Optional[str] = None) -> Dict[str, Any]:
    """Create a new assignment.

    Args:
        user_id: Owner user ID
        title: Short title
        description: Detailed description of the assignment
        participants: Dict mapping character_name -> {"role": "...", "progress": []}
        priority: 1 (urgent) to 5 (background), default 3
        duration_minutes: Optional duration in minutes (alternative to expires_at)
        expires_at: Optional ISO timestamp when the assignment expires
        location_id: Optional location where assignment takes place

    Returns:
        The created assignment dict.
    """
    now = datetime.now()
    assignment_id = uuid.uuid4().hex[:8]

    # Compute expiry
    computed_expires = None
    if expires_at:
        computed_expires = expires_at
    elif duration_minutes and duration_minutes > 0:
        computed_expires = (now + timedelta(minutes=duration_minutes)).isoformat()

    # Normalize participants
    normalized = {}
    for char_name, data in participants.items():
        normalized[char_name] = {
            "role": data.get("role", ""),
            "progress": data.get("progress", []),
        }

    # Auto-detect target count from title/description (e.g. "5 Fotos", "3 Bilder")
    target_count = _extract_target_count(title + " " + description)

    assignment = {
        "id": assignment_id,
        "title": title,
        "description": description,
        "priority": max(1, min(5, priority)),
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": computed_expires,
        "participants": normalized,
        "target_count": target_count,
        "location_id": location_id or "",
        "outfit_hint": outfit_hint or "",
        "source": "api",
    }

    assignments = _load_all()
    assignments.append(assignment)
    _save_all(assignments)
    logger.info("Assignment created: %s '%s' (participants: %s)",
                assignment_id, title, list(normalized.keys()))
    return assignment


def get_assignment(assignment_id: str) -> Optional[Dict[str, Any]]:
    """Get a single assignment by ID."""
    for a in _load_all():
        if a["id"] == assignment_id:
            return a
    return None


def list_assignments(character_name: Optional[str] = None,
    status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List assignments, optionally filtered by character and/or status."""
    assignments = _load_all()
    result = []
    for a in assignments:
        if status and a.get("status") != status:
            continue
        if character_name and character_name not in a.get("participants", {}):
            continue
        result.append(a)
    # Sort by priority (1=highest first), then by created_at
    result.sort(key=lambda x: (x.get("priority", 3), x.get("created_at", "")))
    return result


def update_assignment(assignment_id: str,
    updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update an assignment. Supports partial updates.

    Allowed fields: title, description, priority, status, expires_at, participants.
    When updating participants, existing progress is preserved.
    When reactivating (status -> active), target_count is recalculated.
    """
    assignments = _load_all()
    allowed = {"title", "description", "priority", "status", "expires_at", "participants", "location_id", "outfit_hint"}
    for a in assignments:
        if a["id"] == assignment_id:
            # Merge participants: preserve existing progress
            if "participants" in updates:
                old_participants = a.get("participants", {})
                new_participants = updates["participants"]
                for name, data in new_participants.items():
                    if name in old_participants:
                        # Keep existing progress, update role
                        old_participants[name]["role"] = data.get("role", old_participants[name].get("role", ""))
                    else:
                        old_participants[name] = {"role": data.get("role", ""), "progress": []}
                # Remove participants no longer in list
                for name in list(old_participants.keys()):
                    if name not in new_participants:
                        del old_participants[name]
                a["participants"] = old_participants
                del updates["participants"]

            for key, value in updates.items():
                if key in allowed:
                    a[key] = value

            # Recalculate target_count if title/description changed
            if "title" in updates or "description" in updates:
                a["target_count"] = _extract_target_count(
                    a.get("title", "") + " " + a.get("description", ""))

            # Neustart: wenn status auf 'active' gesetzt UND expires_at geaendert
            # → Fortschritt aller Participants zuruecksetzen
            if updates.get("status") == "active" and "expires_at" in updates:
                for p_data in a.get("participants", {}).values():
                    p_data["progress"] = []
                a.pop("completed_at", None)
                logger.info("Assignment neugestartet: %s '%s' (Fortschritt zurueckgesetzt)",
                            assignment_id, a.get("title"))

            _save_all(assignments)
            logger.info("Assignment updated: %s '%s'", assignment_id, a.get("title"))
            return a
    return None


def delete_assignment(assignment_id: str) -> bool:
    """Delete an assignment by ID."""
    assignments = _load_all()
    before = len(assignments)
    assignments = [a for a in assignments if a["id"] != assignment_id]
    if len(assignments) < before:
        _save_all(assignments)
        logger.info("Assignment deleted: %s", assignment_id)
        return True
    return False


def add_progress(assignment_id: str,
    character_name: str,
    note: str) -> Optional[Dict[str, Any]]:
    """Add a progress note for a specific character on an assignment."""
    assignments = _load_all()
    for a in assignments:
        if a["id"] == assignment_id:
            participants = a.get("participants", {})
            if character_name not in participants:
                return None
            entry = {
                "timestamp": datetime.now().isoformat(),
                "note": note,
            }
            participants[character_name].setdefault("progress", []).append(entry)
            _save_all(assignments)
            logger.info("Assignment %s: progress by %s: %s",
                        assignment_id, character_name, note[:80])
            return a
    return None


def complete_assignment(assignment_id: str,
    character_name: Optional[str] = None,
    note: str = "") -> Optional[Dict[str, Any]]:
    """Mark an assignment as completed.

    If character_name is given, adds a completion note for that character.
    Sets status to 'completed' and records completion timestamp.
    """
    assignments = _load_all()
    for a in assignments:
        if a["id"] == assignment_id:
            if character_name and character_name in a.get("participants", {}):
                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "note": note or "Aufgabe abgeschlossen",
                }
                a["participants"][character_name].setdefault("progress", []).append(entry)
            a["status"] = "completed"
            a["completed_at"] = datetime.now().isoformat()
            _save_all(assignments)
            logger.info("Assignment completed: %s '%s'", assignment_id, a.get("title"))
            return a
    return None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def expire_overdue() -> List[Dict[str, Any]]:
    """Check and expire overdue assignments. Returns list of newly expired ones."""
    assignments = _load_all()
    now = datetime.now()
    expired = []
    changed = False

    for a in assignments:
        if a.get("status") != "active":
            continue
        exp_str = a.get("expires_at")
        if not exp_str:
            continue
        try:
            exp_dt = datetime.fromisoformat(exp_str)
            if now >= exp_dt:
                a["status"] = "expired"
                expired.append(a)
                changed = True
                logger.info("Assignment expired: %s '%s'", a["id"], a.get("title"))
        except (ValueError, TypeError):
            pass

    if changed:
        _save_all(assignments)
    return expired


# ---------------------------------------------------------------------------
# Auto-progress tracking
# ---------------------------------------------------------------------------

# Maps skill/tool names to trackable tool types
TOOL_NAME_MAP = {
    "ImageGeneration": "image",
    "ImageGenerator": "image",
    "WebSearch": "search",
    "SearxSearch": "search",
    "Searx": "search",
    "TalkTo": "talkto",
    "SendNotification": "notification",
    "InstagramPost": "instagram",
    "KnowledgeExtract": "research",
}

# Maps tool types to German labels for progress notes
_TOOL_LABELS = {
    "image": "Foto generiert",
    "search": "Recherche durchgefuehrt",
    "instagram": "Instagram Post erstellt",
    "talkto": "Gespraech gefuehrt",
    "notification": "Benachrichtigung gesendet",
    "research": "Information extrahiert",
}


def auto_track_progress(character_name: str,
    tool_type: str,
    count: int = 1) -> Optional[Dict[str, Any]]:
    """Automatically track tool usage as assignment progress.

    Called after a tool (e.g. ImageGeneration) completes for a character
    that has active assignments. Adds progress entries and auto-completes
    if target_count is reached.

    Returns dict with update info or None if no assignments affected.
    """
    active = list_assignments(character_name=character_name, status="active")
    if not active:
        return None

    label = _TOOL_LABELS.get(tool_type, tool_type)
    result = None
    assignments = _load_all()
    changed = False

    for a_active in active:
        aid = a_active["id"]
        for a in assignments:
            if a["id"] != aid or a.get("status") != "active":
                continue
            participant = a.get("participants", {}).get(character_name)
            if not participant:
                continue

            # Add progress entries
            for _ in range(count):
                participant.setdefault("progress", []).append({
                    "timestamp": datetime.now().isoformat(),
                    "note": label,
                })
            changed = True

            # Check auto-completion: count progress vs target_count
            target = a.get("target_count", 0)
            if target > 0:
                total_progress = len(participant.get("progress", []))
                if total_progress >= target:
                    a["status"] = "completed"
                    a["completed_at"] = datetime.now().isoformat()
                    logger.info("Assignment auto-completed: %s '%s' (%d/%d)",
                                aid, a.get("title"), total_progress, target)

            result = {
                "assignment_id": aid,
                "title": a.get("title", ""),
                "progress_count": len(participant.get("progress", [])),
                "target_count": target,
                "completed": a.get("status") == "completed",
            }
            logger.info("[%s] Auto-progress: %s +%d (%s)", character_name, aid, count, label)

    if changed:
        _save_all(assignments)
    return result


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_assignment_prompt_section(character_name: str) -> str:
    """Build the assignment section for injection into system prompts.

    Returns a formatted string showing active assignments for this character,
    sorted by priority, with role and progress info.
    Returns empty string if no active assignments.
    """
    active = list_assignments(character_name=character_name, status="active")
    if not active:
        return ""

    lines = ["\n== AKTUELLE AUFGABEN (nach Prioritaet) =="]
    for a in active:
        prio_label = PRIORITY_LABELS.get(a.get("priority", 3), "NORMAL")
        lines.append(f"[{prio_label}] {a['title']}")
        lines.append(f"  → {a['description']}")

        # Assignment location
        loc_id = a.get("location_id", "")
        if loc_id:
            try:
                from app.models.world import get_location_name
                loc_name = get_location_name(loc_id) or loc_id
            except Exception:
                loc_name = loc_id
            lines.append(f"  → Ort: {loc_name} — Du MUSST dich dorthin begeben um diese Aufgabe zu erledigen!")

        # Outfit hint
        outfit_hint = a.get("outfit_hint", "")
        if outfit_hint:
            lines.append(f"  → Outfit/Kleidung: {outfit_hint}")

        # This character's role
        participant = a.get("participants", {}).get(character_name, {})
        role = participant.get("role", "")
        if role:
            lines.append(f"  → Deine Rolle: {role}")

        # This character's progress (with target count if set)
        progress = participant.get("progress", [])
        target = a.get("target_count", 0)
        if progress:
            count = len(progress)
            if target > 0:
                lines.append(f"  → Dein Fortschritt: {count} von {target}")
                if count >= target:
                    lines.append(f"  → ZIEL ERREICHT — Aufgabe kann abgeschlossen werden")
            else:
                last_notes = [p["note"] for p in progress[-3:]]
                lines.append(f"  → Dein Fortschritt: {'; '.join(last_notes)}")
        else:
            if target > 0:
                lines.append(f"  → Dein Fortschritt: 0 von {target}")
            else:
                lines.append("  → Dein Fortschritt: (noch nichts)")

        # Other participants' progress
        for other_name, other_data in a.get("participants", {}).items():
            if other_name == character_name:
                continue
            other_progress = other_data.get("progress", [])
            if other_progress:
                last_notes = [p["note"] for p in other_progress[-2:]]
                lines.append(f"  → {other_name}s Fortschritt: {'; '.join(last_notes)}")
            else:
                lines.append(f"  → {other_name}s Fortschritt: (noch nichts)")

        # Deadline
        expires = a.get("expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                lines.append(f"  → Frist: bis {exp_dt.strftime('%d.%m.%Y, %H:%M')}")
            except (ValueError, TypeError):
                pass
        lines.append("")  # Blank line between assignments

    # Andere Participants auflisten fuer Bild-Hinweis
    all_other_names = set()
    for a in active:
        for name in a.get("participants", {}):
            if name != character_name:
                all_other_names.add(name)

    lines.append(
        "WICHTIG fuer Aufgaben:\n"
        "- Wenn eine Aufgabe Fotos/Bilder erfordert, MUSST du das ImageGeneration-Tool benutzen. "
        "Beschreibe NICHT nur was du tust — fuehre es tatsaechlich aus!\n"
        "- Wenn eine Aufgabe Recherche erfordert, MUSST du das WebSearch-Tool benutzen.\n"
        "- Wenn eine Aufgabe Kommunikation erfordert, MUSST du das TalkTo-Tool benutzen.\n"
        "- Der Fortschritt wird automatisch gezaehlt wenn du Tools benutzt."
    )
    if all_other_names:
        lines.append(
            f"\nWICHTIG fuer Bildgenerierung: Wenn du Fotos von anderen Characters machst "
            f"({', '.join(all_other_names)}), beschreibe im ImageGeneration-Input explizit "
            f"WER auf dem Bild sein soll — z.B. '{next(iter(all_other_names))} posiert...'. "
            f"Beschreibe NICHT dich selbst, sondern die Person die fotografiert werden soll!"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Marker extraction
# ---------------------------------------------------------------------------

def extract_assignment_markers(character_name: str,
    response_text: str) -> List[Dict[str, Any]]:
    """Extract [ASSIGNMENT_UPDATE: ...] and [ASSIGNMENT_DONE: ...] markers from response.

    Processes them immediately (adds progress, completes assignment).
    Returns list of processed markers for logging/notification.
    """
    import re
    processed = []

    # [ASSIGNMENT_UPDATE: <id> | <note>]
    for m in re.finditer(r'\[ASSIGNMENT_UPDATE:\s*(\w+)\s*\|\s*([^\]]+)\]', response_text):
        aid, note = m.group(1).strip(), m.group(2).strip()
        result = add_progress(aid, character_name, note)
        if result:
            processed.append({"type": "update", "id": aid, "note": note})
            logger.info("[%s] Assignment update: %s -> %s", character_name, aid, note[:60])

    # [ASSIGNMENT_DONE: <id> | <note>]
    for m in re.finditer(r'\[ASSIGNMENT_DONE:\s*(\w+)\s*\|\s*([^\]]+)\]', response_text):
        aid, note = m.group(1).strip(), m.group(2).strip()
        result = complete_assignment(aid, character_name, note)
        if result:
            processed.append({"type": "done", "id": aid, "note": note})
            logger.info("[%s] Assignment done: %s -> %s", character_name, aid, note[:60])

    return processed


def extract_new_assignment(character_name: str,
    response_text: str) -> Optional[Dict[str, Any]]:
    """Extract [NEW_ASSIGNMENT: ...] marker from chat response and create assignment.

    Format: [NEW_ASSIGNMENT: title | role | description | priority | duration | other1=role1, other2=role2]
    The last field (other participants) is optional.

    Returns created assignment or None.
    """
    import re
    m = re.search(r'\[NEW_ASSIGNMENT:\s*([^\]]+)\]', response_text)
    if not m:
        return None

    parts = [p.strip() for p in m.group(1).split("|")]
    if len(parts) < 5:
        logger.warning("NEW_ASSIGNMENT marker has too few fields: %s", m.group(0))
        return None

    title = parts[0]
    my_role = parts[1]
    description = parts[2]
    try:
        priority = max(1, min(5, int(parts[3])))
    except (ValueError, TypeError):
        priority = 3
    try:
        duration = int(parts[4])
    except (ValueError, TypeError):
        duration = 120

    # Build participants
    participants = {
        character_name: {"role": my_role, "progress": []},
    }

    # Parse optional other participants: "Kira=Model, Enzo=Fotograf"
    if len(parts) > 5 and parts[5].strip():
        for pair in parts[5].split(","):
            pair = pair.strip()
            if "=" in pair:
                other_name, other_role = pair.split("=", 1)
                participants[other_name.strip()] = {
                    "role": other_role.strip(),
                    "progress": [],
                }

    assignment = create_assignment(
        title=title,
        description=description,
        participants=participants,
        priority=priority,
        duration_minutes=duration)
    assignment["source"] = "chat"
    # Re-save with source
    assignments = _load_all()
    for a in assignments:
        if a["id"] == assignment["id"]:
            a["source"] = "chat"
    _save_all(assignments)

    logger.info("[%s] Assignment created via chat: '%s' (participants: %s)",
                character_name, title, list(participants.keys()))
    return assignment


def strip_assignment_tags(text: str) -> str:
    """Remove all assignment marker tags from display text."""
    import re
    text = re.sub(r'\n?\[ASSIGNMENT_(?:UPDATE|DONE):\s*[^\]]+\]', '', text)
    text = re.sub(r'\n?\[NEW_ASSIGNMENT:\s*[^\]]+\]', '', text)
    return text.strip()

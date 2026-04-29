"""Group Chat Session Model.

Manages group conversation sessions where multiple characters at the
same location participate in a shared chat.

Storage: storage/users/{user_id}/group_chats.json
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.models.character import (
    get_character_current_location,
    get_character_profile_image,
    list_available_characters)
from app.core.db import get_connection, transaction

logger = get_logger("group_chat")

from app.core.paths import get_storage_dir

MAX_HISTORY = 200


def _now_iso() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return f"gs_{uuid.uuid4().hex[:10]}"


def _sessions_path() -> Path:
    return get_storage_dir() / "group_chats.json"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _row_to_session(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile in ein Session-Dict.
    Schema: (id, participants, messages, created_at, updated_at)
    """
    session = {"id": row[0], "created_at": row[3], "last_activity": row[4]}
    try:
        session["participants"] = json.loads(row[1] or "[]")
    except Exception:
        session["participants"] = []
    # messages haelt das komplette Session-Dict serialisiert
    try:
        stored = json.loads(row[2] or "{}")
        if isinstance(stored, dict) and "chat_history" in stored:
            session.update(stored)
        elif isinstance(stored, list):
            session["chat_history"] = stored
    except Exception:
        session["chat_history"] = []
    if "chat_history" not in session:
        session["chat_history"] = []
    if "active" not in session:
        session["active"] = True
    if "location_id" not in session:
        session["location_id"] = ""
    return session


def load_sessions() -> List[Dict[str, Any]]:
    """Laedt alle Group-Chat-Sessions aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, participants, messages, created_at, updated_at "
            "FROM group_chats ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_session(r) for r in rows]
    except Exception as e:
        logger.warning("load_sessions DB-Fehler: %s", e)
        path = _sessions_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("sessions", [])
            except Exception:
                pass
        return []


def save_sessions(sessions: List[Dict[str, Any]]):
    """Speichert alle Sessions in die DB (Upsert) und JSON-Backup."""
    now = _now_iso()
    try:
        with transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM group_chats"
            ).fetchall()}
            new_ids = {s.get("id") for s in sessions if s.get("id")}

            for sid in existing_ids - new_ids:
                conn.execute("DELETE FROM group_chats WHERE id=?", (sid,))

            for session in sessions:
                sid = session.get("id")
                if not sid:
                    continue
                # Speichere das komplette Session-Dict in messages als JSON
                session_copy = dict(session)
                # Cap chat_history
                if len(session_copy.get("chat_history", [])) > MAX_HISTORY:
                    session_copy["chat_history"] = session_copy["chat_history"][-MAX_HISTORY:]
                conn.execute("""
                    INSERT INTO group_chats (id, participants, messages, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        participants=excluded.participants,
                        messages=excluded.messages,
                        updated_at=excluded.updated_at
                """, (
                    sid,
                    json.dumps(session.get("participants", []), ensure_ascii=False),
                    json.dumps(session_copy, ensure_ascii=False),
                    session.get("created_at", now),
                    session.get("last_activity", now),
                ))
    except Exception as e:
        logger.error("save_sessions DB-Fehler: %s", e)

    # JSON-Backup
    try:
        path = _sessions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"sessions": sessions, "last_updated": now}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_active_session(location_id: str) -> Optional[Dict[str, Any]]:
    """Find an active session at the given location."""
    for s in load_sessions():
        if s.get("location_id") == location_id and s.get("active", True):
            return s
    return None


def create_group_session(location_id: str,
    participants: List[str]) -> Dict[str, Any]:
    """Create a new group session (or reuse existing one at location)."""
    sessions = load_sessions()

    # Reuse existing active session at this location
    for s in sessions:
        if s.get("location_id") == location_id and s.get("active", True):
            # Update participants
            s["participants"] = participants
            s["last_activity"] = _now_iso()
            save_sessions(sessions)
            return s

    session = {
        "id": _new_id(),
        "location_id": location_id,
        "participants": participants,
        "created_at": _now_iso(),
        "last_activity": _now_iso(),
        "active": True,
        "chat_history": [],
    }
    sessions.append(session)
    save_sessions(sessions)
    return session


def save_group_message(session_id: str,
    role: str,
    content: str,
    character: str = "",
    whisper_to: str = "") -> None:
    """Append a message to the session's chat history.

    whisper_to: Wenn gesetzt, ist die Nachricht ein Fluestern an diesen Character.
    Andere Teilnehmer sehen nur einen Platzhalter statt des Inhalts.
    """
    sessions = load_sessions()
    for s in sessions:
        if s["id"] == session_id:
            history = s.setdefault("chat_history", [])
            msg: Dict[str, Any] = {
                "role": role,
                "content": content,
                "timestamp": _now_iso(),
            }
            if character:
                msg["character"] = character
            if whisper_to:
                msg["whisper_to"] = whisper_to
            history.append(msg)
            # Cap history
            if len(history) > MAX_HISTORY:
                s["chat_history"] = history[-MAX_HISTORY:]
            s["last_activity"] = _now_iso()
            save_sessions(sessions)
            return
    logger.warning("Session %s not found for user %s", session_id)


def get_group_chat_history(session_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """Return the last N messages from a session."""
    sessions = load_sessions()
    for s in sessions:
        if s["id"] == session_id:
            history = s.get("chat_history", [])
            return history[-limit:] if limit else history
    return []


def close_session(session_id: str) -> bool:
    """Mark a session as inactive so a fresh one can be created."""
    sessions = load_sessions()
    for s in sessions:
        if s["id"] == session_id:
            s["active"] = False
            s["closed_at"] = _now_iso()
            save_sessions(sessions)
            return True
    return False


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def get_characters_at_location(location_id: str
) -> List[Dict[str, str]]:
    """Return all characters currently at the given location.

    Reuses the same logic as the /characters/at-location endpoint.
    """
    from app.models.world import resolve_location

    loc = resolve_location(location_id)
    loc_id = loc.get("id", "") if loc else ""

    all_chars = list_available_characters()
    result = []
    for name in all_chars:
        char_loc = get_character_current_location(name)
        if char_loc and (char_loc == loc_id or char_loc == location_id):
            profile_img = get_character_profile_image(name)
            result.append({
                "name": name,
                "profile_image": profile_img or "",
                "avatar_url": (
                    f"/characters/{name}/images/{profile_img}"
                    if profile_img else ""
                ),
            })
    return result

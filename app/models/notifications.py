"""Notification storage and CRUD operations.

Notifications are stored per-world in der DB-Tabelle `notifications`.
Each notification has: id, character, content, timestamp, read, type, metadata.
A sliding window keeps the row count bounded (max MAX_NOTIFICATIONS entries).
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_NOTIFICATIONS = 200

from app.core.db import get_connection, transaction


def _row_to_notification(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile in ein Notification-Dict.
    Schema: (id, ts, kind, title, body, read, meta)
    """
    meta = {}
    try:
        meta = json.loads(row[6] or "{}")
    except Exception:
        pass
    return {
        "id": meta.get("str_id", str(row[0])),
        "character": meta.get("character", ""),
        "content": row[4] or "",
        "timestamp": row[1] or "",
        "read": bool(row[5]),
        "type": row[2] or "message",
        "metadata": meta.get("metadata", {}),
        "_db_id": row[0],
    }


def _load() -> List[Dict[str, Any]]:
    """Laedt alle Notifications aus der DB (neueste zuerst)."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, ts, kind, title, body, read, meta "
            "FROM notifications ORDER BY ts DESC LIMIT ?",
            (MAX_NOTIFICATIONS,),
        ).fetchall()
        return [_row_to_notification(r) for r in rows]
    except Exception:
        return []


def create_notification(character: str,
    content: str,
    notification_type: str = "message",
    metadata: Optional[Dict[str, Any]] = None) -> str:
    """Create a new notification. Returns the notification ID."""
    nid = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    meta_blob = json.dumps({
        "str_id": nid,
        "character": character,
        "metadata": metadata or {},
    }, ensure_ascii=False)
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO notifications (ts, kind, title, body, read, meta) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (now, notification_type, character, content, meta_blob),
            )
            # Enforce sliding window: delete oldest beyond MAX_NOTIFICATIONS
            conn.execute(
                "DELETE FROM notifications WHERE id NOT IN ("
                "  SELECT id FROM notifications ORDER BY ts DESC LIMIT ?"
                ")",
                (MAX_NOTIFICATIONS,),
            )
    except Exception:
        pass
    return nid


def _visible_to(notif: Dict[str, Any], allowed: set) -> bool:
    """Eine Notification ist fuer den User sichtbar, wenn er entweder den
    Absender (character) oder den Empfaenger (metadata.to) steuern darf.
    Fuer send_message-Trigger ist character=Absender, metadata.to=Empfaenger.
    Fuer alle anderen Types (thought, event_resolved, …) ist character der
    ausloesende Character — metadata.to existiert dort nicht.
    """
    if notif.get("character") in allowed:
        return True
    to = (notif.get("metadata") or {}).get("to")
    if to and to in allowed:
        return True
    return False


def get_notifications(limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    character_whitelist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return notifications (newest first), with optional filtering.

    character_whitelist: wenn gesetzt, nur Notifications die Absender ODER
    Empfaenger in dieser Liste haben. Absender steht in character/title,
    Empfaenger (bei send_message) in metadata.to.
    """
    try:
        conn = get_connection()
        where = []
        params: list = []
        if unread_only:
            where.append("read=0")
        # Character-Filter in Python (weil meta.to in JSON-Blob liegt).
        # Pre-Filter auf title ist nicht ausreichend: fuer eingehende DMs
        # muss auch metadata.to matchen. Wir ueberholen das Limit etwas
        # (3x) damit nach dem Python-Filter genug uebrig bleibt.
        effective_limit = limit * 3 if character_whitelist is not None else limit
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([effective_limit, offset])
        rows = conn.execute(
            "SELECT id, ts, kind, title, body, read, meta FROM notifications"
            + clause + " ORDER BY ts DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        items = [_row_to_notification(r) for r in rows]
        if character_whitelist is not None:
            if not character_whitelist:
                return []
            allowed = set(character_whitelist)
            items = [n for n in items if _visible_to(n, allowed)]
        return items[:limit]
    except Exception:
        return []


def get_unread_count(character_whitelist: Optional[List[str]] = None) -> int:
    """Return number of unread notifications (lightweight for polling)."""
    try:
        conn = get_connection()
        if character_whitelist is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read=0"
            ).fetchone()
            return row[0] if row else 0
        if not character_whitelist:
            return 0
        # Wie get_notifications: wir muessen Absender ODER Empfaenger pruefen,
        # und Empfaenger (metadata.to) liegt im JSON-Blob. Zaehlen in Python.
        rows = conn.execute(
            "SELECT meta FROM notifications WHERE read=0"
        ).fetchall()
        allowed = set(character_whitelist)
        count = 0
        for (meta_str,) in rows:
            try:
                meta = json.loads(meta_str or "{}")
            except Exception:
                continue
            char = meta.get("character", "")
            to = (meta.get("metadata") or {}).get("to", "")
            if char in allowed or (to and to in allowed):
                count += 1
        return count
    except Exception:
        return 0


def mark_read(notification_id: str) -> bool:
    """Mark a single notification as read. Returns True if found."""
    try:
        with transaction() as conn:
            # str_id lives in meta JSON, need to scan or use meta field
            # To avoid JSON scan, load all and find db_id
            rows = conn.execute(
                "SELECT id, meta FROM notifications"
            ).fetchall()
            for row_id, meta_str in rows:
                try:
                    meta = json.loads(meta_str or "{}")
                    if meta.get("str_id") == notification_id:
                        conn.execute(
                            "UPDATE notifications SET read=1 WHERE id=?",
                            (row_id,),
                        )
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


def mark_all_read() -> int:
    """Mark all notifications as read. Returns count of newly marked."""
    try:
        with transaction() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read=0"
            ).fetchone()
            count = count_row[0] if count_row else 0
            if count:
                conn.execute("UPDATE notifications SET read=1 WHERE read=0")
            return count
    except Exception:
        return 0


def delete_notification(notification_id: str) -> bool:
    """Delete a single notification. Returns True if found."""
    try:
        with transaction() as conn:
            rows = conn.execute(
                "SELECT id, meta FROM notifications"
            ).fetchall()
            for row_id, meta_str in rows:
                try:
                    meta = json.loads(meta_str or "{}")
                    if meta.get("str_id") == notification_id:
                        conn.execute(
                            "DELETE FROM notifications WHERE id=?",
                            (row_id,),
                        )
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

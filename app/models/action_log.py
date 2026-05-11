"""Character Action Log — persistente Historie aller `act`-Skill-Aufrufe.

Speichert pro Character den User-Input, die Storyteller-Antwort, ob ein
Event durch die Aktion aufgeloest wurde, und welches. Quelle fuer die
Action-Historie im Chat-UI (siehe `/characters/{name}/actions`).

Schema-Definition in `app/core/world_db_schema.py` (Tabelle
`character_action_log`). Diese Datei kapselt nur die CRUD-Helfer, damit
der Skill-Code keinen Roh-SQL braucht.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.db import get_connection, transaction
from app.core.log import get_logger

logger = get_logger("action_log")


def insert_action_log(*,
    character_name: str,
    scope: str,
    location_id: str,
    room_id: str,
    user_input: str,
    storyteller_response: str,
    event_resolved: bool = False,
    event_id: Optional[str] = None) -> Optional[int]:
    """Legt einen Action-Log-Eintrag an und gibt die ID zurueck (oder None)."""
    if not character_name or not user_input:
        return None
    try:
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO character_action_log
                   (character_name, scope, location_id, room_id,
                    user_input, storyteller_response, event_resolved,
                    event_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (character_name, scope or "here", location_id or "",
                 room_id or "", user_input or "",
                 storyteller_response or "",
                 1 if event_resolved else 0,
                 event_id or "",
                 datetime.now().isoformat()))
            return int(cur.lastrowid) if cur.lastrowid else None
    except Exception as e:
        logger.warning("insert_action_log fehlgeschlagen fuer %s: %s",
                       character_name, e)
        return None


def list_action_log(character_name: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Listet die letzten Action-Eintraege eines Characters, neueste zuerst."""
    if not character_name:
        return []
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT id, character_name, scope, location_id, room_id,
                      user_input, storyteller_response, event_resolved,
                      event_id, created_at
               FROM character_action_log
               WHERE character_name=?
               ORDER BY created_at DESC LIMIT ?""",
            (character_name, max(1, int(limit or 30)))
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "character_name": r[1],
                "scope": r[2],
                "location_id": r[3],
                "room_id": r[4],
                "user_input": r[5],
                "storyteller_response": r[6],
                "event_resolved": bool(r[7]),
                "event_id": r[8] or None,
                "timestamp": r[9],
            })
        return out
    except Exception as e:
        logger.warning("list_action_log fehlgeschlagen fuer %s: %s",
                       character_name, e)
        return []

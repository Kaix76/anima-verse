"""Location Events - Situative Ereignisse pro Ort.

Speichert kurzlebige Ereignisse die allen Characteren am selben Ort
in den System-Prompt injiziert werden.

Storage: storage/users/{username}/events.json
"""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# TTL-Optionen in Stunden (Label -> Stunden, 0 = kein Ablauf)
TTL_OPTIONS = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "48h": 48,
    "7d": 168,
    "0": 0,
}
DEFAULT_TTL_HOURS = 24

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("events")

from app.core.paths import get_storage_dir


def _get_events_file() -> Path:
    sd = get_storage_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "events.json"


def _row_to_event(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile (id, ts, payload) in ein Event-Dict."""
    # row: (id, ts, payload)
    payload = {}
    try:
        payload = json.loads(row[2] or "{}")
    except Exception:
        pass
    # Ensure the event's "id" string field stays consistent
    if "id" not in payload:
        payload["id"] = str(row[0])
    if "created_at" not in payload:
        payload["created_at"] = row[1] or ""
    return payload


def _load_events() -> List[Dict[str, Any]]:
    """Laedt alle Events aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, ts, payload FROM events "
            "WHERE kind='world_event' ORDER BY ts ASC"
        ).fetchall()
        events = [_row_to_event(r) for r in rows]
        return events
    except Exception as e:
        logger.warning("_load_events DB-Fehler: %s", e)
        # Fallback: JSON-Datei
        path = _get_events_file()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            events = data.get("events", [])
        except Exception:
            return []
        # Migration: alte Events ohne TTL-Felder ergaenzen (nur im JSON-Fallback)
        for evt in events:
            if "ttl_hours" not in evt:
                evt["ttl_hours"] = DEFAULT_TTL_HOURS
            if "expires_at" not in evt and evt.get("ttl_hours", 0) > 0:
                try:
                    created = datetime.fromisoformat(evt["created_at"])
                    evt["expires_at"] = (created + timedelta(hours=evt["ttl_hours"])).isoformat()
                except (ValueError, TypeError, KeyError):
                    pass
        return events


def _save_events(events: List[Dict[str, Any]]):
    """Speichert Events in die DB (Upsert via string-id in payload).

    Das events-Schema hat nur (id INTEGER, ts, kind, character_name, payload).
    Die event-string-ID wird im payload gespeichert; zum Loeschen brauchen wir
    eine Lookup-Runde via payload-JSON-Extraktion.
    """
    try:
        with transaction() as conn:
            # Alle vorhandenen world_event-Zeilen laden (id-integer -> event-string-id)
            existing_rows = conn.execute(
                "SELECT id, payload FROM events WHERE kind='world_event'"
            ).fetchall()
            # event_str_id -> db_int_id
            existing_map: Dict[str, int] = {}
            for row_id, row_payload in existing_rows:
                try:
                    p = json.loads(row_payload or "{}")
                    str_id = p.get("id", str(row_id))
                    existing_map[str_id] = row_id
                except Exception:
                    existing_map[str(row_id)] = row_id

            new_ids = {e.get("id") for e in events if e.get("id")}

            # Geloeschte Events entfernen
            for str_id, db_id in existing_map.items():
                if str_id not in new_ids:
                    conn.execute("DELETE FROM events WHERE id=?", (db_id,))

            # Upsert: vorhandene aktualisieren, neue einfuegen
            for evt in events:
                str_id = evt.get("id")
                if not str_id:
                    continue
                ts = evt.get("created_at", datetime.now().isoformat())
                payload_str = json.dumps(evt, ensure_ascii=False)
                if str_id in existing_map:
                    conn.execute(
                        "UPDATE events SET ts=?, payload=? WHERE id=?",
                        (ts, payload_str, existing_map[str_id]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO events (ts, kind, character_name, payload) "
                        "VALUES (?, 'world_event', NULL, ?)",
                        (ts, payload_str),
                    )
    except Exception as e:
        logger.error("_save_events DB-Fehler: %s", e)


def add_event(text: str,
    location_id: Optional[str] = None,
    ttl_hours: Optional[int] = None,
    category: str = "",
    escalation_of: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Erstellt ein neues Ereignis.

    location_id=None -> globales Event.
    category: ambient, social, disruption, danger (leer = unkategorisiert)
    escalation_of: Event-ID des Vorgaenger-Events (Eskalationskette)
    metadata: Optionales Dict fuer zusaetzliche Event-Daten (z.B. Secret-Hint-Infos).
    """
    if ttl_hours is None:
        ttl_hours = DEFAULT_TTL_HOURS
    now = datetime.now()
    events = _load_events()
    event = {
        "id": f"evt_{uuid.uuid4().hex[:8]}",
        "text": text.strip(),
        "location_id": location_id or None,
        "category": category or "",
        "ttl_hours": ttl_hours,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours > 0 else None,
    }
    if escalation_of:
        event["escalation_of"] = escalation_of
    if metadata:
        event["metadata"] = metadata
    events.append(event)
    _save_events(events)
    logger.info("Event erstellt: %s [%s] (location=%s, ttl=%dh)",
                event["id"], category or "?", location_id, ttl_hours)
    return event


def _is_expired(event: Dict[str, Any]) -> bool:
    """Prueft ob ein Event abgelaufen ist."""
    expires_at = event.get("expires_at")
    if not expires_at:
        return False  # kein Ablauf (ttl=0)
    try:
        return datetime.now() > datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return False


def _cleanup_expired(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Entfernt abgelaufene Events und speichert wenn noetig."""
    active = [e for e in events if not _is_expired(e)]
    if len(active) < len(events):
        # Event-gekoppelte Block-Rules mit aufraeumen.
        try:
            from app.models.rules import delete_rules_by_event
            for e in events:
                if _is_expired(e) and e.get("id"):
                    delete_rules_by_event(e["id"])
        except Exception as _e:
            logger.debug("delete_rules_by_event(cleanup) fehlgeschlagen: %s", _e)
        _save_events(active)
        logger.info("%d abgelaufene Events entfernt", len(events) - len(active))
    return active


def list_events(location_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Listet aktive Events. Optional gefiltert nach Location."""
    events = _cleanup_expired(_load_events())
    if location_id is not None:
        events = [e for e in events if e.get("location_id") == location_id or e.get("location_id") is None]
    return events


def get_all_events() -> List[Dict[str, Any]]:
    """Alle aktiven Events (abgelaufene werden automatisch entfernt)."""
    return _cleanup_expired(_load_events())


RESOLVED_TTL_HOURS = 2  # Geloeste Events bleiben noch 2h sichtbar


def resolve_event(event_id: str,
    resolved_by: str = "", resolved_text: str = "") -> Optional[Dict[str, Any]]:
    """Markiert ein Event als geloest.

    - Nur disruption/danger Events koennen geloest werden
    - TTL wird auf RESOLVED_TTL_HOURS (2h) ab jetzt verkuerzt
    - resolved_by: Name des Characters der es geloest hat
    - resolved_text: Kurze Beschreibung der Loesung
    """
    events = _load_events()
    for evt in events:
        if evt.get("id") != event_id:
            continue
        if evt.get("category", "") not in ("disruption", "danger"):
            return None  # Nur lösbare Events
        if evt.get("resolved"):
            return evt  # Bereits gelöst

        now = datetime.now()
        evt["resolved"] = True
        evt["resolved_by"] = resolved_by
        evt["resolved_text"] = resolved_text
        evt["resolved_at"] = now.isoformat()
        # TTL auf 2h ab jetzt verkuerzen
        evt["expires_at"] = (now + timedelta(hours=RESOLVED_TTL_HOURS)).isoformat()

        _save_events(events)
        logger.info("Event geloest: %s von %s — %s", event_id, resolved_by, resolved_text[:60])
        # Sofortiges Aufraeumen der gekoppelten Block-Rules — der Weg ist
        # frei, sobald geloest, nicht erst nach dem Resolved-TTL.
        try:
            from app.models.rules import delete_rules_by_event
            delete_rules_by_event(event_id)
        except Exception as _e:
            logger.debug("delete_rules_by_event(resolve) fehlgeschlagen: %s", _e)
        return evt
    return None


def record_attempt(event_id: str,
    who: str, text: str, outcome: str, reason: str = "",
    joint_with: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Protokolliert einen Loesungsversuch am Event.

    outcome: "success" | "fail"
    joint_with: weitere beteiligte Characters (gemeinsame Versuche)

    Resolution-Schema auf dem Event:
      resolution = {
        "attempts": [{when, who, text, outcome, reason, joint_with}, ...],
        "last_attempt_at": iso-timestamp,
      }
    """
    events = _load_events()
    for evt in events:
        if evt.get("id") != event_id:
            continue
        now = datetime.now()
        resolution = evt.setdefault("resolution", {"attempts": [], "last_attempt_at": None})
        resolution["attempts"].append({
            "when": now.isoformat(),
            "who": who,
            "text": (text or "")[:500],
            "outcome": outcome,
            "reason": (reason or "")[:200],
            "joint_with": joint_with or [],
        })
        resolution["last_attempt_at"] = now.isoformat()
        _save_events(events)
        logger.info("Event-Attempt %s: %s von %s (%s)", event_id, outcome, who, reason[:60])
        return evt
    return None


def delete_event(event_id: str) -> bool:
    """Loescht ein Event anhand der ID."""
    events = _load_events()
    new_events = [e for e in events if e.get("id") != event_id]
    if len(new_events) < len(events):
        _save_events(new_events)
        logger.info("Event geloescht: %s", event_id)
        try:
            from app.models.rules import delete_rules_by_event
            delete_rules_by_event(event_id)
        except Exception as _e:
            logger.debug("delete_rules_by_event(delete) fehlgeschlagen: %s", _e)
        return True
    return False


def build_events_prompt_section(location_id: Optional[str] = None) -> str:
    """Baut den Events-Abschnitt fuer den System-Prompt.

    Sichtbarkeit nach Kritikalitaet:
    - ambient/social: Nur am gleichen Ort (+ globale Events)
    - disruption: Am gleichen Ort (+ globale)
    - danger: ALLE danger-Events, unabhaengig vom Ort
    """
    if not location_id:
        return ""

    # Events am aktuellen Ort (alle Kategorien)
    local_events = list_events(location_id=location_id)
    local_ids = {e.get("id") for e in local_events}

    # Nachbar-Locations ueber Karten-Grid ermitteln
    try:
        from app.models.world import get_neighbor_location_ids
        neighbor_ids = set(get_neighbor_location_ids(location_id))
    except Exception:
        neighbor_ids = set()

    # Disruption von Nachbarn + Danger von ueberall
    all_events = get_all_events()
    nearby_events = []
    for e in all_events:
        if e.get("id") in local_ids:
            continue
        evt_loc = e.get("location_id", "")
        cat = e.get("category", "")
        if cat == "danger":
            nearby_events.append(e)  # Danger: ueberall sichtbar
        elif cat == "disruption" and evt_loc in neighbor_ids:
            nearby_events.append(e)  # Disruption: nur Nachbar-Orte

    if not local_events and not nearby_events:
        return ""

    lines = []

    # Lokale Events
    if local_events:
        lines.append("Events at your location:")
        for evt in local_events:
            ts = _format_event_timestamp(evt.get("created_at", ""))
            prefix = f"[{ts}] " if ts else ""
            cat = evt.get("category", "")
            cat_tag = f"[{cat.upper()}] " if cat else ""
            if evt.get("resolved"):
                who = evt.get("resolved_by", "someone")
                how = evt.get("resolved_text", "")
                resolution = f" — {who}: {how}" if how else f" — resolved by {who}"
                lines.append(f"- {prefix}[RESOLVED] {evt['text']}{resolution}")
            else:
                lines.append(f"- {prefix}{cat_tag}{evt['text']}")

    # Nearby Events (Disruption von Nachbarn + Danger von ueberall)
    if nearby_events:
        lines.append("Events nearby (you can hear/sense them from your location):")
        for evt in nearby_events:
            ts = _format_event_timestamp(evt.get("created_at", ""))
            prefix = f"[{ts}] " if ts else ""
            cat = evt.get("category", "").upper()
            lines.append(f"- {prefix}[{cat}] {evt['text']}")

    return "\n" + "\n".join(lines)


def _format_event_timestamp(iso_ts: str) -> str:
    """Kompaktes, LLM-lesbares Datum."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return ""
    now = datetime.now()
    delta = now.date() - dt.date()
    time_str = dt.strftime("%H:%M")
    if delta.days == 0:
        return f"heute {time_str}"
    elif delta.days == 1:
        return f"gestern {time_str}"
    elif delta.days < 7:
        return f"vor {delta.days} Tagen"
    else:
        return dt.strftime("%d.%m. %H:%M")

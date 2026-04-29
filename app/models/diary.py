"""Diary / Timeline — Generierte Tagesansicht pro Character.

Aggregiert Daten aus bestehenden Quellen (keine Duplikation):
- mood_history.json → Stimmungsaenderungen
- Chat-History → Gedanken-Eintraege (Tool-Calls, Aktionen)
- instagram_feed.json → Posts und Kommentare
- assignments.json → Fortschritt und Abschluesse

Storage: storage/users/{username}/characters/{char}/diary.json
  → Wird bei Bedarf neu generiert (Generate-Button), nicht realtime geschrieben.
  → Einzige Ausnahme: daily_summary (LLM-generiert, wird persistent gespeichert).
"""
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("diary")


# ---------------------------------------------------------------------------
# Entry types & icons
# ---------------------------------------------------------------------------

ENTRY_TYPES = {
    "mood":               "Stimmung",
    "location":           "Ortswechsel",
    "activity":           "Aktivitaet",
    "condition":          "Zustand",
    "effects":            "Effekte",
    "thought":            "Gedanke",
    "assignment_update":  "Aufgaben-Update",
    "assignment_done":    "Aufgabe erledigt",
    "daily_summary":      "Tagesrueckblick",
    "instagram_post":     "Instagram Post",
    "instagram_comment":  "Instagram Kommentar",
    "forced_action":      "Erzwungen",
    "access_denied":      "Zugang verweigert",
    "event_resolved":     "Ereignis geloest",
    "event_attempt":      "Loesungsversuch",
}

ENTRY_ICONS = {
    "mood":               "\U0001f3ad",   # 🎭
    "location":           "\U0001f4cd",   # 📍
    "activity":           "\u26a1",       # ⚡
    "condition":          "\U0001f525",   # 🔥
    "effects":            "\U0001f4ca",   # 📊
    "thought":            "\U0001f9e0",   # 🧠
    "assignment_update":  "\U0001f4cb",   # 📋
    "assignment_done":    "\u2705",       # ✅
    "daily_summary":      "\U0001f4d6",   # 📖
    "instagram_post":     "\U0001f4f8",   # 📸
    "instagram_comment":  "\U0001f4ac",   # 💬
    "forced_action":      "\u26a0\ufe0f",  # ⚠️
    "access_denied":      "\U0001f6ab",   # 🚫
    "event_resolved":     "\U0001f6a8",   # 🚨
    "event_attempt":      "\U0001f6e0️",  # 🛠️
}


# ---------------------------------------------------------------------------
# Storage (only for daily_summary persistence)
# ---------------------------------------------------------------------------

def _get_diary_path(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "diary.json"


def _load_stored(character_name: str) -> List[Dict[str, Any]]:
    """Load stored diary entries (currently only daily_summary) from DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, ts, content, tags, meta FROM diary_entries "
            "WHERE character_name=? ORDER BY ts ASC",
            (character_name,),
        ).fetchall()
        entries = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[4] or "{}")
            except Exception:
                pass
            entry = {
                "id": meta.get("str_id", str(r[0])),
                "type": meta.get("type", "daily_summary"),
                "content": r[2] or "",
                "timestamp": r[1] or "",
                "metadata": meta.get("metadata", {}),
                "_db_id": r[0],
            }
            entries.append(entry)
        return entries
    except Exception as e:
        logger.warning("_load_stored diary DB-Fehler fuer %s: %s", character_name, e)
        # Fallback: JSON-Datei
        path = _get_diary_path(character_name)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error("Failed to load diary for %s: %s", character_name, exc)
            return []


def _save_stored(character_name: str, entries: List[Dict[str, Any]]) -> None:
    """Save diary entries to DB and JSON backup."""
    try:
        with transaction() as conn:
            # Load existing db_id -> str_id mapping
            existing_rows = conn.execute(
                "SELECT id, meta FROM diary_entries WHERE character_name=?",
                (character_name,),
            ).fetchall()
            db_to_str: Dict[int, str] = {}
            str_to_db: Dict[str, int] = {}
            for db_id, meta_str in existing_rows:
                try:
                    m = json.loads(meta_str or "{}")
                    str_id = m.get("str_id", str(db_id))
                except Exception:
                    str_id = str(db_id)
                db_to_str[db_id] = str_id
                str_to_db[str_id] = db_id

            new_str_ids = {e.get("id") for e in entries if e.get("id")}

            # Remove deleted entries
            for db_id, str_id in db_to_str.items():
                if str_id not in new_str_ids:
                    conn.execute("DELETE FROM diary_entries WHERE id=?", (db_id,))

            # Upsert
            for entry in entries:
                str_id = entry.get("id")
                if not str_id:
                    continue
                ts = entry.get("timestamp", datetime.now().isoformat())
                content = entry.get("content", "")
                tags = json.dumps([entry.get("type", "daily_summary")], ensure_ascii=False)
                meta_blob = json.dumps({
                    "str_id": str_id,
                    "type": entry.get("type", "daily_summary"),
                    "metadata": entry.get("metadata", {}),
                }, ensure_ascii=False)

                if str_id in str_to_db:
                    conn.execute(
                        "UPDATE diary_entries SET ts=?, content=?, tags=?, meta=? WHERE id=?",
                        (ts, content, tags, meta_blob, str_to_db[str_id]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO diary_entries (character_name, ts, content, tags, meta) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (character_name, ts, content, tags, meta_blob),
                    )
    except Exception as e:
        logger.error("_save_stored diary DB-Fehler fuer %s: %s", character_name, e)

    # JSON-Backup
    try:
        path = _get_diary_path(character_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        clean = [{k: v for k, v in e.items() if k != "_db_id"} for e in entries]
        path.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def add_summary(character_name: str, content: str, date: str) -> Dict[str, Any]:
    """Store a daily summary (LLM-generated). Only type that gets persisted."""
    entry = {
        "id": uuid.uuid4().hex[:8],
        "type": "daily_summary",
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "metadata": {"date": date},
    }
    stored = _load_stored(character_name)
    stored.append(entry)
    _save_stored(character_name, stored)
    return entry


def has_daily_summary(character_name: str, date: Optional[str] = None) -> bool:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    stored = _load_stored(character_name)
    return any(
        e.get("type") == "daily_summary" and e.get("timestamp", "").startswith(date)
        for e in stored
    )


# ---------------------------------------------------------------------------
# Source collectors — each returns List[Dict] with type/content/timestamp
# ---------------------------------------------------------------------------

def _collect_mood(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Mood changes from mood_history DB table."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT ts, mood FROM mood_history "
            "WHERE character_name=? AND ts LIKE ? ORDER BY ts ASC",
            (character_name, f"{date}%"),
        ).fetchall()
        result = []
        for r in rows:
            mood = r[1] or ""
            if mood:
                result.append({
                    "type": "mood",
                    "content": f"Stimmung: {mood}",
                    "timestamp": r[0] or "",
                })
        return result
    except Exception as e:
        logger.debug("Mood collect DB error: %s — falling back to JSON", e)
    # Fallback: JSON-Datei
    from app.models.character import get_character_dir
    path = get_character_dir(character_name) / "mood_history.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries_raw = data.get("entries", []) if isinstance(data, dict) else data
        result = []
        for e in entries_raw:
            ts = e.get("timestamp", "")
            if not ts.startswith(date):
                continue
            mood = e.get("mood", "")
            if mood:
                result.append({
                    "type": "mood",
                    "content": f"Stimmung: {mood}",
                    "timestamp": ts,
                })
        return result
    except Exception as e:
        logger.debug("Mood collect error: %s", e)
        return []


def get_state_history(character_name: str,
    entry_type: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """Gibt die letzten N Eintraege aus state_history zurueck (neueste zuerst).

    entry_type: Filter auf "activity", "location", "condition" etc. Leer = alle.
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT ts, state_json FROM state_history "
            "WHERE character_name=? ORDER BY ts DESC LIMIT ?",
            (character_name, limit * 4 if entry_type else limit),
        ).fetchall()
        entries = []
        for r in rows:
            state = {}
            try:
                state = json.loads(r[1] or "{}")
            except Exception:
                pass
            if not state:
                state = {"timestamp": r[0] or "", "type": "", "value": ""}
            if entry_type and state.get("type") != entry_type:
                continue
            entries.append({
                "timestamp": state.get("timestamp", r[0] or ""),
                "type": state.get("type", ""),
                "value": state.get("value", ""),
                "metadata": state.get("metadata", {}),
            })
            if len(entries) >= limit:
                break
        return entries
    except Exception as e:
        logger.debug("get_state_history DB error: %s — falling back to JSON", e)
    # Fallback: JSON-Datei
    from app.models.character import get_character_dir
    path = get_character_dir(character_name) / "state_history.json"
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if entry_type:
            entries = [e for e in entries if e.get("type") == entry_type]
        return list(reversed(entries[-limit:]))
    except Exception:
        return []


def _process_state_entry(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Verarbeitet einen einzelnen State-History-Eintrag in einen Diary-Eintrag."""
    ts = e.get("timestamp", "")
    change_type = e.get("type", "")
    value = e.get("value", "")
    meta = e.get("metadata") or {}

    if change_type == "location":
        loc_name = value
        try:
            from app.models.world import get_location_name
            resolved = get_location_name(value)
            if resolved:
                loc_name = resolved
        except Exception:
            pass
        return {"type": "location", "content": f"Ortswechsel: {loc_name}", "timestamp": ts}

    elif change_type == "activity":
        detail = meta.get("detail", "")
        partner = meta.get("partner", "")
        display = f"{value} ({detail})" if (detail and detail.lower() != value.lower()) else value
        if partner:
            display = f"{display} mit {partner}"
        entry: Dict[str, Any] = {"type": "activity", "content": f"Aktivitaet: {display}", "timestamp": ts}
        if partner:
            entry["metadata"] = {"partner": partner}
        return entry

    elif change_type == "access_denied":
        loc_name = meta.get("location_name") or value
        reason = meta.get("reason", "")
        content = f"Wollte zu {loc_name}" + (f" — {reason}" if reason else "")
        return {"type": "access_denied", "content": content, "timestamp": ts, "metadata": meta}

    elif change_type == "effects":
        changes = meta.get("changes", {})
        if changes:
            entry_meta: Dict[str, Any] = {"changes": changes}
            if meta.get("elapsed_minutes"):
                entry_meta["elapsed_minutes"] = meta["elapsed_minutes"]
            return {"type": "effects", "content": f"Effekte ({value})", "timestamp": ts, "metadata": entry_meta}

    return None


def _collect_state_changes(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Location and activity changes from state_history DB table."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT ts, state_json FROM state_history "
            "WHERE character_name=? AND ts LIKE ? ORDER BY ts ASC",
            (character_name, f"{date}%"),
        ).fetchall()
        result = []
        for r in rows:
            state = {}
            try:
                state = json.loads(r[1] or "{}")
            except Exception:
                pass
            if not state:
                state = {"timestamp": r[0] or "", "type": "", "value": ""}
            entry_dict = {
                "timestamp": state.get("timestamp", r[0] or ""),
                "type": state.get("type", ""),
                "value": state.get("value", ""),
                "metadata": state.get("metadata", {}),
            }
            processed = _process_state_entry(entry_dict)
            if processed:
                result.append(processed)
        return result
    except Exception as e:
        logger.debug("_collect_state_changes DB error: %s — falling back to JSON", e)
    # Fallback: JSON-Datei
    from app.models.character import get_character_dir
    path = get_character_dir(character_name) / "state_history.json"
    if not path.exists():
        return []
    try:
        raw_entries = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for e in raw_entries:
            ts = e.get("timestamp", "")
            if not ts.startswith(date):
                continue
            # Adapt JSON entry format to internal format
            entry_dict = {
                "timestamp": ts,
                "type": e.get("type", ""),
                "value": e.get("value", ""),
                "metadata": e.get("metadata") or {},
            }
            processed = _process_state_entry(entry_dict)
            if processed:
                result.append(processed)
        return result
    except Exception as e:
        logger.debug("State history collect error: %s", e)
        return []


def _collect_chat_events(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Thought-driven messages and TalkTo results from chat history.

    Scans this character's own chats (outgoing thoughts + TalkTo)
    AND other characters' chats (incoming TalkTo where this character was the target).
    """
    from app.models.character import get_character_dir, get_user_characters_dir

    result = []

    # --- Own chat: outgoing thoughts + TalkTo ---
    chat_dir = get_character_dir(character_name) / "chats"
    if chat_dir.exists():
        for chat_file in chat_dir.glob(f"*{date}*"):
            _parse_chat_file(chat_file, date, character_name, result, is_own=True)

    # --- Other characters' chats: incoming TalkTo ---
    chars_dir = get_user_characters_dir()
    if chars_dir.exists():
        for char_dir in chars_dir.iterdir():
            if not char_dir.is_dir() or char_dir.name == character_name:
                continue
            other_chat_dir = char_dir / "chats"
            if not other_chat_dir.exists():
                continue
            for chat_file in other_chat_dir.glob(f"*{date}*"):
                _parse_chat_file(chat_file, date, character_name, result,
                                 is_own=False, sender_name=char_dir.name)

    return result


def _parse_chat_file(
    chat_file: Path,
    date: str,
    character_name: str,
    result: List[Dict[str, Any]],
    is_own: bool = True,
    sender_name: str = "") -> None:
    """Parse a chat file for diary-relevant entries."""
    try:
        messages = json.loads(chat_file.read_text(encoding="utf-8"))
    except Exception:
        return

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        ts = msg.get("timestamp", "")
        if not ts.startswith(date):
            continue

        if is_own:
            # Thought-generated messages
            thought_match = re.match(
                r'\[Gedanken-Nachricht\s*\|\s*([^|]*?)\s*\|\s*[^\]]*\]\s*(.*)',
                content, re.DOTALL
            )
            if thought_match:
                location = thought_match.group(1).strip()
                text = thought_match.group(2).strip()
                text = re.sub(r'<tool[^>]*>.*?(?:</tool>|$)', '', text, flags=re.DOTALL).strip()
                if text and text.upper() != "SKIP":
                    result.append({
                        "type": "thought",
                        "content": text,
                        "timestamp": ts,
                        "metadata": {"location": location},
                    })


def _collect_instagram(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Instagram posts and comments — reads from DB events table or feed.json fallback."""
    try:
        from app.models.instagram import load_feed
        feed = load_feed()
    except Exception:
        from app.core.paths import get_storage_dir
        feed_path = get_storage_dir() / "instagram_feed.json"
        if not feed_path.exists():
            return []
        try:
            feed = json.loads(feed_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    result = []
    try:
        for post in feed:
            ts = post.get("timestamp", "")
            agent = post.get("agent_name", "")

            # Posts by this character
            if agent == character_name and ts.startswith(date):
                caption = post.get("caption", "")
                result.append({
                    "type": "instagram_post",
                    "content": f"Instagram Post: {caption}",
                    "timestamp": ts,
                    "metadata": {"post_id": post.get("id", "")},
                })

            # Comments by this character on any post
            for comment in post.get("comments", []):
                cts = comment.get("timestamp", "")
                if comment.get("author") == character_name and cts.startswith(date):
                    post_author = post.get("agent_name", "")
                    result.append({
                        "type": "instagram_comment",
                        "content": f"Kommentar bei {post_author}: {comment.get('text', '')}",
                        "timestamp": cts,
                        "metadata": {"post_id": post.get("id", "")},
                    })
    except Exception as e:
        logger.debug("Instagram collect error: %s", e)
    return result


def _collect_assignments(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Assignment progress from assignments.json."""
    from app.models.assignments import _load_all
    result = []
    try:
        for a in _load_all():
            participant = a.get("participants", {}).get(character_name)
            if not participant:
                continue
            title = a.get("title", a.get("id", ""))
            for p in participant.get("progress", []):
                ts = p.get("timestamp", "")
                if not ts.startswith(date):
                    continue
                note = p.get("note", "")
                # Detect completion vs update
                is_done = a.get("status") == "completed" and p == participant["progress"][-1]
                etype = "assignment_done" if is_done else "assignment_update"
                result.append({
                    "type": etype,
                    "content": f"[{title}] {note}",
                    "timestamp": ts,
                    "metadata": {"assignment_id": a.get("id", "")},
                })
    except Exception as e:
        logger.debug("Assignment collect error: %s", e)
    return result


def _enrich_with_relationships(
    entries: List[Dict[str, Any]], character_name: str,
    date: str) -> None:
    """Tag existing diary entries with relationship sentiment if a matching
    relationship history entry exists (matched by timestamp within 120s)."""
    try:
        from app.models.relationship import get_character_relationships
        rels = get_character_relationships(character_name)
    except Exception:
        return

    # Build list of (datetime, target, sentiment_delta) from relationship history
    rel_events = []
    name_lower = character_name.lower()
    for rel in rels:
        is_a = rel.get("character_a", "").lower() == name_lower
        other = rel.get("character_b", "") if is_a else rel.get("character_a", "")

        # Fallback for old history entries without sentiment_delta:
        # estimate from overall sentiment / interaction_count
        overall_sent = rel.get("sentiment_a_to_b", 0.0) if is_a else rel.get("sentiment_b_to_a", 0.0)
        count = max(rel.get("interaction_count", 1), 1)
        estimated_delta = overall_sent / count

        for h in rel.get("history", []):
            ts = h.get("timestamp", "")
            if not ts.startswith(date):
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            stored_delta = h.get("sentiment_delta")
            # Use stored delta if present, otherwise estimate
            delta = stored_delta if stored_delta is not None else estimated_delta
            rel_events.append({
                "dt": dt,
                "target": other,
                "sentiment_delta": delta,
            })

    if not rel_events:
        return

    # For each diary entry, find closest relationship event within 120s.
    # Collect all candidates first, then assign each rel_event only to the
    # single closest diary entry to avoid duplicate tags.
    candidates = []  # (entry_index, rel_event_index, time_diff)
    for ei, entry in enumerate(entries):
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        try:
            entry_dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        for ri, rev in enumerate(rel_events):
            diff = abs((entry_dt - rev["dt"]).total_seconds())
            if diff <= 120:
                candidates.append((ei, ri, diff))

    # Sort by time_diff so the closest match wins
    candidates.sort(key=lambda c: c[2])
    used_entries: set = set()
    used_events: set = set()
    for ei, ri, _ in candidates:
        if ei in used_entries or ri in used_events:
            continue
        rev = rel_events[ri]
        delta = rev["sentiment_delta"]
        if abs(delta) < 0.005:
            used_events.add(ri)
            continue
        meta = entries[ei].setdefault("metadata", {})
        meta["relationship_target"] = rev["target"]
        meta["relationship_sentiment"] = round(delta, 2)
        used_entries.add(ei)
        used_events.add(ri)


def _collect_event_resolutions(character_name: str, date: str) -> List[Dict[str, Any]]:
    """Sammelt Loesungsversuche + geloeste Events in denen der Character aktiv war.

    Quelle: `events`-Tabelle, pro Event gibt es `resolution.attempts: [...]` mit
    {when, who, text, outcome, reason, joint_with}. Sowie das finale
    `resolved_by` / `resolved_text`. Alle mit Timestamp im Tagesbereich werden
    als Diary-Entries gerendert.
    """
    result: List[Dict[str, Any]] = []
    try:
        from app.models.events import get_all_events
        events = get_all_events()
    except Exception:
        return result
    for evt in events:
        resolution = evt.get("resolution") or {}
        attempts = resolution.get("attempts") or []
        evt_text = (evt.get("text") or "").strip()
        for att in attempts:
            who = att.get("who") or ""
            joint = att.get("joint_with") or []
            if character_name != who and character_name not in joint:
                continue
            when = att.get("when") or ""
            if not when or not str(when).startswith(date):
                continue
            solution_text = (att.get("text") or "").strip()
            outcome = att.get("outcome") or "fail"
            joint_txt = f" (mit {', '.join(joint)})" if joint else ""
            if outcome == "success":
                content = f"Ereignis geloest{joint_txt}: {evt_text[:120]} — Aktion: {solution_text[:200]}"
                etype = "event_resolved"
            else:
                reason = (att.get("reason") or "").strip()
                tail = f" — Erfolglos: {reason[:100]}" if reason else " — Erfolglos"
                content = f"Loesungsversuch{joint_txt}: {evt_text[:120]} — Aktion: {solution_text[:180]}{tail}"
                etype = "event_attempt"
            result.append({
                "type": etype,
                "content": content,
                "timestamp": str(when),
                "metadata": {"event_id": evt.get("id", ""),
                             "category": evt.get("category", ""),
                             "joint_with": joint,
                             "outcome": outcome},
            })
    return result


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate_for_day(character_name: str,
    date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Generate diary entries for a day by pulling from all sources.

    Merges data from mood_history, chat history, instagram, assignments.
    Also includes stored daily_summary entries.
    Returns chronologically sorted list.
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    entries = []
    entries.extend(_collect_mood(character_name, date))
    entries.extend(_collect_state_changes(character_name, date))
    entries.extend(_collect_chat_events(character_name, date))
    entries.extend(_collect_instagram(character_name, date))
    entries.extend(_collect_event_resolutions(character_name, date))
    entries.extend(_collect_assignments(character_name, date))

    # Add stored daily summaries
    for e in _load_stored(character_name):
        if e.get("type") == "daily_summary" and e.get("timestamp", "").startswith(date):
            entries.append(e)

    # Sort chronologically
    entries.sort(key=lambda x: x.get("timestamp", ""))

    # Tag entries that had relationship impact
    _enrich_with_relationships(entries, character_name, date)

    return entries


def get_available_dates_fast(character_name: str) -> List[str]:
    """Fast date detection — checks DB timestamps and file timestamps."""
    from app.models.character import get_character_dir
    dates = set()

    # Mood history dates from DB
    try:
        conn = get_connection()
        for r in conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) FROM mood_history WHERE character_name=?",
            (character_name,),
        ).fetchall():
            if r[0] and len(r[0]) == 10:
                dates.add(r[0])
    except Exception:
        # Fallback: JSON
        mood_path = get_character_dir(character_name) / "mood_history.json"
        if mood_path.exists():
            try:
                data = json.loads(mood_path.read_text(encoding="utf-8"))
                for e in (data.get("entries", []) if isinstance(data, dict) else []):
                    ts = e.get("timestamp", "")
                    if len(ts) >= 10:
                        dates.add(ts[:10])
            except Exception:
                pass

    # State history dates from DB
    try:
        conn = get_connection()
        for r in conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) FROM state_history WHERE character_name=?",
            (character_name,),
        ).fetchall():
            if r[0] and len(r[0]) == 10:
                dates.add(r[0])
    except Exception:
        # Fallback: JSON
        state_path = get_character_dir(character_name) / "state_history.json"
        if state_path.exists():
            try:
                for e in json.loads(state_path.read_text(encoding="utf-8")):
                    ts = e.get("timestamp", "")
                    if len(ts) >= 10:
                        dates.add(ts[:10])
            except Exception:
                pass

    # Chat history dates from DB
    try:
        conn = get_connection()
        for r in conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) FROM chat_messages WHERE character_name=?",
            (character_name,),
        ).fetchall():
            if r[0] and len(r[0]) == 10:
                dates.add(r[0])
    except Exception:
        # Fallback: Chat files
        chat_dir = get_character_dir(character_name) / "chats"
        if chat_dir.exists():
            for f in chat_dir.glob("*.json"):
                m = re.search(r'(\d{4}-\d{2}-\d{2})', f.name)
                if m:
                    dates.add(m.group(1))

    # Stored diary dates (daily summaries)
    for e in _load_stored(character_name):
        ts = e.get("timestamp", "")
        if len(ts) >= 10:
            dates.add(ts[:10])

    return sorted(dates, reverse=True)


def build_daily_summary_input(character_name: str, date: Optional[str] = None) -> str:
    """Build text summary of day's events for LLM consumption.

    Deduplicates consecutive identical entries (e.g. same mood repeated),
    truncates long thought messages, and limits total length to fit
    within small model context windows.
    """
    entries = generate_for_day(character_name, date)
    entries = [e for e in entries if e.get("type") != "daily_summary"]
    if not entries:
        return ""

    lines = []
    last_content = ""
    for e in entries:
        ts = e.get("timestamp", "")
        time_str = ts[11:16] if len(ts) >= 16 else ""
        etype = ENTRY_TYPES.get(e.get("type", ""), e.get("type", ""))
        content = e.get("content", "")

        # Deduplicate consecutive identical content (e.g. same mood 16x)
        if content == last_content:
            continue
        last_content = content

        # Truncate long thought messages for summary input
        if len(content) > 200:
            content = content[:200] + "..."

        lines.append(f"- {time_str} [{etype}] {content}")

    result = "\n".join(lines)
    # Hard limit to ~3000 chars to fit in small model context
    if len(result) > 3000:
        result = result[:3000] + "\n... (gekuerzt)"
    return result

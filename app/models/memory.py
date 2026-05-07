"""Character Memory System - Langzeitgedaechtnis pro Character.

Ersetzt das alte Knowledge-System mit kontextbasiertem Retrieval,
natuerlichem Decay und Memory-Typen (episodic, semantic, commitment).

Storage: world.db — Tabellen memories, mood_history
"""
import json
import math
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("memory")


# ---------------------------------------------------------------------------
# MemoryEntry helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _row_to_entry(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Row in das Legacy-Entry-Dict-Format."""
    d = dict(row)
    # Legacy-Felder aus meta wiederherstellen
    meta = {}
    try:
        meta = json.loads(d.get("meta") or "{}")
    except Exception:
        pass
    entry = {
        "id": d.get("id", ""),
        "timestamp": d.get("ts", ""),
        "memory_type": d.get("tier", "semantic"),
        "content": d.get("content", ""),
        "tags": [],
        "source_ids": [],
        **meta,  # Alle meta-Felder (context, importance, access_count, etc.)
    }
    try:
        entry["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        pass
    try:
        entry["source_ids"] = json.loads(d.get("source_ids") or "[]")
    except Exception:
        pass
    # Row-ID als Fallback-ID
    if not entry.get("id"):
        entry["id"] = f"mem_{d.get('id', '')}"
    return entry


def _entry_to_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Konvertiert ein Entry-Dict in DB-Felder."""
    meta_keys = ("context", "importance", "access_count", "last_accessed",
                 "decay_factor", "related_character")
    meta = {k: entry[k] for k in meta_keys if k in entry}
    return {
        "tier": entry.get("memory_type", "semantic"),
        "ts": entry.get("timestamp", _now_iso()),
        "content": entry.get("content", ""),
        "source_ids": json.dumps(entry.get("source_ids", []), ensure_ascii=False),
        "tags": json.dumps(entry.get("tags", []), ensure_ascii=False),
        "meta": json.dumps(meta, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Memory CRUD
# ---------------------------------------------------------------------------

def load_memories(character_name: str) -> List[Dict[str, Any]]:
    """Laedt alle Memory-Eintraege eines Characters aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM memories WHERE character_name=? ORDER BY ts DESC",
            (character_name,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]
    except Exception as e:
        logger.error("load_memories Fehler fuer %s: %s", character_name, e)
        return []


def save_memories(character_name: str, entries: List[Dict[str, Any]]):
    """Ersetzt alle Memories eines Characters in der DB.

    Wird aufgerufen wenn eine oder mehrere Memories geaendert wurden
    (access_count, decay_factor, entfernte Eintraege etc.).
    """
    try:
        # Bestimme welche Entries neu/geaendert sind via ID-Vergleich
        conn = get_connection()
        existing_ids = {
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM memories WHERE character_name=?", (character_name,)
            ).fetchall()
        }
        with transaction() as conn:
            entry_ids_seen = set()
            for entry in entries:
                row = _entry_to_row(entry)
                raw_id = entry.get("id", "")
                # Numeric row-id aus "mem_N" oder direkte int-ID
                row_id = None
                if raw_id and str(raw_id).isdigit():
                    row_id = int(raw_id)
                elif raw_id and raw_id.startswith("mem_") and raw_id[4:].isdigit():
                    row_id = int(raw_id[4:])

                if row_id and str(row_id) in existing_ids:
                    # Update bestehender Row
                    conn.execute("""
                        UPDATE memories
                        SET tier=?, ts=?, content=?, source_ids=?, tags=?, meta=?
                        WHERE id=? AND character_name=?
                    """, (
                        row["tier"], row["ts"], row["content"],
                        row["source_ids"], row["tags"], row["meta"],
                        row_id, character_name,
                    ))
                    entry_ids_seen.add(str(row_id))
                elif not raw_id or raw_id.startswith("mem_") and not (raw_id[4:].isdigit()):
                    # Neuer Eintrag (hex-ID aus altem System)
                    cur = conn.execute("""
                        INSERT INTO memories
                        (character_name, tier, ts, content, source_ids, tags, meta)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (character_name, row["tier"], row["ts"], row["content"],
                          row["source_ids"], row["tags"], row["meta"]))
                    entry_ids_seen.add(str(cur.lastrowid))
                else:
                    # Unbekannte oder neue Entry
                    cur = conn.execute("""
                        INSERT INTO memories
                        (character_name, tier, ts, content, source_ids, tags, meta)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (character_name, row["tier"], row["ts"], row["content"],
                          row["source_ids"], row["tags"], row["meta"]))
                    entry_ids_seen.add(str(cur.lastrowid))

            # Geloeschte Entries entfernen
            to_delete = existing_ids - entry_ids_seen
            for del_id in to_delete:
                conn.execute(
                    "DELETE FROM memories WHERE id=? AND character_name=?",
                    (del_id, character_name)
                )
    except Exception as e:
        logger.error("save_memories Fehler fuer %s: %s", character_name, e)


def add_memory(character_name: str,
    content: str,
    memory_type: str = "semantic",
    importance: int = 3,
    tags: Optional[List[str]] = None,
    context: str = "",
    related_character: str = "",
    timestamp: str = "",
    extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fuegt eine neue Memory hinzu.

    Cap-Enforcement (MEMORY_MAX_SEMANTIC) erfolgt ausschliesslich im
    Background-Konsolidierungsjob (memory_service._cleanup_phase) — niemals
    synchron beim Schreiben, damit Aufrufer (Chat, Room-Entry, ...) nicht
    blockieren.
    """
    content = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', content).strip()
    if not content:
        return {}

    entry = {
        "id": _new_id(),
        "timestamp": timestamp or _now_iso(),
        "memory_type": memory_type,
        "content": content,
        "context": context,
        "importance": importance,
        "access_count": 0,
        "last_accessed": "",
        "decay_factor": 1.0,
        "tags": tags or [],
        "related_character": related_character,
    }
    try:
        meta = {k: entry[k] for k in ("context", "importance", "access_count",
                "last_accessed", "decay_factor", "related_character") if k in entry}
        if extra_meta:
            meta.update(extra_meta)
        with transaction() as conn:
            conn.execute("""
                INSERT INTO memories
                (character_name, tier, ts, content, source_ids, tags, meta)
                VALUES (?, ?, ?, ?, '[]', ?, ?)
            """, (
                character_name,
                memory_type,
                entry["timestamp"],
                content,
                json.dumps(tags or [], ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False),
            ))
    except Exception as e:
        logger.error("add_memory DB-Fehler fuer %s: %s", character_name, e)
    logger.debug("+1 memory [%s] fuer %s: %s", memory_type, character_name, content[:80])
    return entry


def delete_memory(character_name: str, entry_id: str) -> bool:
    """Loescht eine einzelne Memory."""
    try:
        # entry_id kann hex-String (mem_xxx) oder numeric sein
        conn = get_connection()
        # Versuche zuerst als numerische Row-ID
        if str(entry_id).isdigit():
            cur = conn.execute(
                "DELETE FROM memories WHERE id=? AND character_name=?",
                (int(entry_id), character_name),
            )
        elif entry_id.startswith("mem_") and entry_id[4:].isdigit():
            cur = conn.execute(
                "DELETE FROM memories WHERE id=? AND character_name=?",
                (int(entry_id[4:]), character_name),
            )
        else:
            # Hex-ID: suche in meta oder content (Legacy-Fallback)
            rows = conn.execute(
                "SELECT id FROM memories WHERE character_name=? AND meta LIKE ?",
                (character_name, f'%"{entry_id}"%'),
            ).fetchall()
            if not rows:
                return False
            cur = conn.execute(
                "DELETE FROM memories WHERE id=? AND character_name=?",
                (rows[0][0], character_name),
            )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.error("delete_memory Fehler fuer %s id=%s: %s", character_name, entry_id, e)
        return False


def clear_memories(character_name: str) -> int:
    """Loescht alle Memories. Gibt Anzahl zurueck."""
    try:
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE character_name=?", (character_name,)
        ).fetchone()[0]
        if count > 0:
            conn.execute(
                "DELETE FROM memories WHERE character_name=?", (character_name,)
            )
            conn.commit()
        return count
    except Exception as e:
        logger.error("clear_memories Fehler fuer %s: %s", character_name, e)
        return 0


def upsert_relationship_memory(character_name: str,
    related_character: str,
    new_fact: str,
    max_facts: int = 5,
    replace_prefix: str = "") -> Dict[str, Any]:
    """Aktualisiert den Beziehungs-Eintrag zu einem anderen Character.

    Aequivalent zu knowledge.upsert_character_relationship(), aber in memories.json.
    Pro related_character gibt es genau EINEN Eintrag (memory_type="semantic", tag="relationship").
    """
    new_fact = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', new_fact).strip()
    if not new_fact:
        return {}

    entries = load_memories(character_name)

    # Bestehenden Relationship-Eintrag suchen
    existing = None
    for e in entries:
        if (
            "relationship" in e.get("tags", [])
            and e.get("related_character") == related_character
        ):
            existing = e
            break

    if existing:
        facts = [f.strip() for f in existing.get("content", "").split("\n") if f.strip()]
        if replace_prefix:
            facts = [f for f in facts if not f.startswith(replace_prefix)]
        if new_fact not in facts:
            facts.append(new_fact)
        if len(facts) > max_facts:
            facts = facts[-max_facts:]
        existing["content"] = "\n".join(facts)
        existing["timestamp"] = _now_iso()
        save_memories(character_name, entries)
        return existing
    else:
        return add_memory(
            character_name=character_name,
            content=new_fact,
            memory_type="semantic",
            importance=2,
            tags=["relationship"],
            related_character=related_character)


def delete_memories_by_source(character_name: str, tag: str, context: str = ""
) -> int:
    """Loescht Memories mit passendem Tag (und optional context)."""
    try:
        conn = get_connection()
        if context:
            # context ist in meta gespeichert — lade und filtere
            rows = conn.execute(
                "SELECT id, tags, meta FROM memories WHERE character_name=?",
                (character_name,),
            ).fetchall()
            to_del = []
            for r in rows:
                try:
                    tags_list = json.loads(r[1] or "[]")
                    meta_d = json.loads(r[2] or "{}")
                    if tag in tags_list and meta_d.get("context") == context:
                        to_del.append(r[0])
                except Exception:
                    pass
            if to_del:
                conn.execute(
                    f"DELETE FROM memories WHERE id IN ({','.join('?'*len(to_del))})",
                    to_del,
                )
                conn.commit()
            removed = len(to_del)
        else:
            # Tag-Match via JSON — SQLite json_each
            rows = conn.execute(
                "SELECT id, tags FROM memories WHERE character_name=?",
                (character_name,),
            ).fetchall()
            to_del = []
            for r in rows:
                try:
                    if tag in json.loads(r[1] or "[]"):
                        to_del.append(r[0])
                except Exception:
                    pass
            if to_del:
                conn.execute(
                    f"DELETE FROM memories WHERE id IN ({','.join('?'*len(to_del))})",
                    to_del,
                )
                conn.commit()
            removed = len(to_del)
        if removed > 0:
            logger.info("%d memories (tag=%s) geloescht fuer %s", removed, tag, character_name)
        return removed
    except Exception as e:
        logger.error("delete_memories_by_source Fehler fuer %s: %s", character_name, e)
        return 0


def get_memories_by_tag(character_name: str,
    tag: str,
    related_character: str = "",
    limit: int = 0) -> List[Dict[str, Any]]:
    """Gibt Memories zurueck die einen bestimmten Tag haben."""
    entries = load_memories(character_name)
    filtered = [e for e in entries if tag in e.get("tags", [])]
    if related_character:
        meta_key = "related_character"
        filtered = [e for e in filtered if e.get(meta_key) == related_character]
    if limit:
        filtered = filtered[-limit:]
    return filtered


# ---------------------------------------------------------------------------
# Scoring & Retrieval
# ---------------------------------------------------------------------------

def _recency_boost(age_days: float) -> float:
    """Gibt einen Multiplikator fuer sehr aktuelle Memories zurueck.

    Aktuelle Daten (< 2 Tage) bekommen einen deutlichen Boost,
    damit sie gegenueber aelteren Eintraegen bevorzugt werden.

    Kurve:
      0-1   Tage  ->  2.0  (doppelt gewichtet)
      1-2   Tage  ->  1.5-2.0  (linear abfallend)
      2-3   Tage  ->  1.0-1.5  (linear abfallend)
      3+    Tage  ->  1.0  (kein Boost)
    """
    if age_days <= 1.0:
        return 2.0
    elif age_days <= 2.0:
        # Linear von 2.0 auf 1.5
        return 2.0 - 0.5 * (age_days - 1.0)
    elif age_days <= 3.0:
        # Linear von 1.5 auf 1.0
        return 1.5 - 0.5 * (age_days - 2.0)
    else:
        return 1.0


def _compute_decay(entry: Dict[str, Any]) -> float:
    """Berechnet aktuellen Decay basierend auf Alter und Access-Count."""
    try:
        ts = datetime.fromisoformat(entry.get("timestamp", ""))
    except (ValueError, TypeError):
        return 0.5
    age_days = max(0, (datetime.now() - ts).total_seconds() / 86400)
    # Base decay: halbes Leben nach 30 Tagen
    base_decay = math.exp(-0.023 * age_days)
    # Access-Bonus: haeufig abgerufene Memories zerfallen langsamer
    access_bonus = min(0.3, entry.get("access_count", 0) * 0.05)
    return min(1.0, max(0.05, base_decay + access_bonus))


def _keyword_overlap(text: str, query: str) -> float:
    """Einfacher Keyword-Overlap-Score zwischen Text und Query."""
    if not text or not query:
        return 0.0
    text_words = set(text.lower().split())
    query_words = set(query.lower().split())
    # Stoppwoerter ignorieren
    stop = {"ich", "du", "er", "sie", "es", "wir", "ihr", "und", "oder", "der",
            "die", "das", "ein", "eine", "ist", "hat", "war", "wird", "the",
            "a", "an", "is", "was", "are", "i", "you", "he", "she", "it",
            "we", "they", "and", "or", "in", "on", "at", "to", "for", "of",
            "with", "nicht", "nicht", "aber", "auch", "dann", "wenn", "so",
            "ja", "nein", "mein", "dein", "sein", "ihr", "have", "had",
            "been", "be", "do", "did", "will", "would", "can", "could",
            "that", "this", "what", "how", "my", "your", "his", "her"}
    text_words -= stop
    query_words -= stop
    if not query_words:
        return 0.0
    overlap = text_words & query_words
    return len(overlap) / len(query_words)


def retrieve_relevant_memories(character_name: str,
    current_message: str = "",
    max_results: int = 0) -> List[Dict[str, Any]]:
    """Ruft die relevantesten Memories ab, gescort nach Kontext.

    Score = importance * decay * (1 + keyword_relevance + type_bonus)
    """
    if max_results <= 0:
        max_results = int(os.environ.get("MEMORY_MAX_PROMPT_ENTRIES", "20"))

    entries = load_memories(character_name)
    if not entries:
        return []

    scored = []
    for entry in entries:
        decay = _compute_decay(entry)
        importance = entry.get("importance", 3)

        # Keyword-Relevanz
        search_text = entry.get("content", "") + " " + " ".join(entry.get("tags", []))
        relevance = _keyword_overlap(search_text, current_message) if current_message else 0.0

        # Typ-Bonus: nur OFFENE Commitments bekommen Bonus
        type_bonus = 0.0
        if entry.get("memory_type") == "commitment":
            if "completed" not in entry.get("tags", []):
                type_bonus = 0.3
            # Completed commitments: kein Bonus, normales Decay
        elif entry.get("memory_type") == "episodic":
            type_bonus = 0.1

        # Recency-Boost: aktuelle Daten (< 2 Tage) deutlich bevorzugen
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            age_days = max(0, (datetime.now() - ts).total_seconds() / 86400)
        except (ValueError, TypeError):
            age_days = 30.0  # Unbekanntes Alter = kein Boost
        recency = _recency_boost(age_days)

        score = importance * decay * recency * (1.0 + relevance * 2.0 + type_bonus)
        scored.append((score, entry, decay))

    # Sortieren nach Score
    scored.sort(key=lambda x: x[0], reverse=True)

    # Top-N zurueckgeben und Access-Count aktualisieren
    result = []
    changed = False
    for score, entry, decay in scored[:max_results]:
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["last_accessed"] = _now_iso()
        entry["decay_factor"] = round(decay, 3)
        result.append(entry)
        changed = True

    if changed:
        save_memories(character_name, entries)

    return result


# ---------------------------------------------------------------------------
# Prompt-Section Builder (ersetzt build_knowledge_prompt_section)
# ---------------------------------------------------------------------------

def _format_memory_timestamp(iso_ts: str) -> str:
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


def build_memory_prompt_section(character_name: str,
    partner_name: str = "",
    current_message: str = "") -> str:
    """Baut den Memory-Abschnitt fuer den System-Prompt.

    Gruppiert nach Typ: episodic, semantic, commitment, relationships.
    Kontextbasiert abgerufen statt statisch Top-N.

    `partner_name` ist der aktuelle Konversationspartner (Charaktername).
    Wenn gesetzt, fuegt der Header einen Disambiguierungs-Hinweis ein,
    damit das LLM bei TalkTo nicht den falschen Adressaten als "User"
    interpretiert.
    """
    memories = retrieve_relevant_memories(character_name,
        current_message=current_message)
    if not memories:
        return ""

    # Gruppieren nach memory_type
    episodic = []
    semantic = []
    commitments = []
    relationships = []

    for mem in memories:
        ts = _format_memory_timestamp(mem.get("timestamp", ""))
        ts_prefix = f"({ts}) " if ts else ""
        related = mem.get("related_character", "")
        content = mem.get("content", "")

        # Recency-Marker fuer sehr aktuelle Eintraege (< 2 Tage)
        recency_marker = ""
        try:
            mem_ts = datetime.fromisoformat(mem.get("timestamp", ""))
            mem_age_days = (datetime.now() - mem_ts).total_seconds() / 86400
            if mem_age_days <= 1.0:
                recency_marker = "[AKTUELL] "
            elif mem_age_days <= 2.0:
                recency_marker = "[KÜRZLICH] "
        except (ValueError, TypeError):
            pass

        mtype = mem.get("memory_type", "semantic")
        if mtype == "episodic":
            episodic.append(f"- {recency_marker}{ts_prefix}{content}")
        elif mtype == "commitment":
            # Completed commitments nicht im Prompt anzeigen
            if "completed" in mem.get("tags", []):
                continue
            commitments.append(f"- {recency_marker}{ts_prefix}{content}")
        elif related:
            relationships.append(f"- {recency_marker}{ts_prefix}(about {related}) {content}")
        else:
            semantic.append(f"- {recency_marker}{ts_prefix}{content}")

    parts = []
    header = "\nYour memories"
    if partner_name:
        header += (
            f" (you are currently talking with {partner_name} — other names "
            f"are other characters you have met)"
        )
    header += (
        ". Only state what you actually know, never invent details.\n"
        "Entries marked [AKTUELL] or [KÜRZLICH] are very recent — "
        "prioritize them over older entries when relevant:"
    )
    parts.append(header)

    if episodic:
        parts.append("\nWhat you've experienced together:")
        parts.extend(episodic)

    if semantic:
        parts.append("\nFacts you remember:")
        parts.extend(semantic)

    if commitments:
        parts.append("\nOpen commitments & plans:")
        parts.extend(commitments)

    if relationships:
        parts.append("\nAbout other characters:")
        parts.extend(relationships)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Mood History
# ---------------------------------------------------------------------------

def load_mood_history(character_name: str) -> List[Dict[str, Any]]:
    """Laedt Mood-History eines Characters aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT ts, mood, source FROM mood_history "
            "WHERE character_name=? ORDER BY ts ASC",
            (character_name,),
        ).fetchall()
        return [{"timestamp": r[0], "mood": r[1], "source": r[2]} for r in rows]
    except Exception as e:
        logger.error("load_mood_history Fehler fuer %s: %s", character_name, e)
        return []


def save_mood_history(character_name: str, entries: List[Dict[str, Any]]):
    """Ersetzt Mood-History in der DB (max 500 Eintraege, aelteste werden geloescht)."""
    max_entries = int(os.environ.get("MOOD_HISTORY_MAX_ENTRIES", "500"))
    # Nur letzte max_entries behalten — delete excess
    try:
        conn = get_connection()
        total = conn.execute(
            "SELECT COUNT(*) FROM mood_history WHERE character_name=?",
            (character_name,),
        ).fetchone()[0]
        if total > max_entries:
            # Aelteste loeschen
            excess = total - max_entries
            conn.execute("""
                DELETE FROM mood_history WHERE id IN (
                    SELECT id FROM mood_history
                    WHERE character_name=?
                    ORDER BY ts ASC LIMIT ?
                )
            """, (character_name, excess))
            conn.commit()
    except Exception as e:
        logger.error("save_mood_history Trim-Fehler fuer %s: %s", character_name, e)


def record_mood(character_name: str, mood: str, source: str = ""):
    """Zeichnet einen Mood-Datenpunkt auf."""
    if not mood:
        return
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO mood_history (character_name, ts, mood, source)
                VALUES (?, ?, ?, ?)
            """, (character_name, _now_iso(), mood.strip().lower(), source))
        logger.debug("Mood recorded for %s: %s", character_name, mood)
        # Trim falls noetig
        max_entries = int(os.environ.get("MOOD_HISTORY_MAX_ENTRIES", "500"))
        conn = get_connection()
        total = conn.execute(
            "SELECT COUNT(*) FROM mood_history WHERE character_name=?",
            (character_name,),
        ).fetchone()[0]
        if total > max_entries:
            excess = total - max_entries
            conn.execute("""
                DELETE FROM mood_history WHERE id IN (
                    SELECT id FROM mood_history
                    WHERE character_name=?
                    ORDER BY ts ASC LIMIT ?
                )
            """, (character_name, excess))
            conn.commit()
    except Exception as e:
        logger.error("record_mood Fehler fuer %s: %s", character_name, e)


# ---------------------------------------------------------------------------
# Migration: Knowledge -> Memory
# ---------------------------------------------------------------------------

def migrate_knowledge_to_memories(character_name: str) -> int:
    """Migriert bestehende knowledge.json Eintraege in die memories-Tabelle.

    Wird nur einmal ausgefuehrt (wenn keine Memories fuer den Character existieren).
    Bestehende knowledge.json bleibt erhalten (Backup).
    """
    # Bereits Daten in DB? -> Abbruch
    try:
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE character_name=?", (character_name,)
        ).fetchone()[0]
        if count > 0:
            return 0  # Bereits migriert
    except Exception:
        return 0

    from app.core.paths import get_storage_dir
    knowledge_path = get_storage_dir() / "characters" / character_name / "knowledge.json"
    if not knowledge_path.exists():
        return 0

    try:
        data = json.loads(knowledge_path.read_text(encoding="utf-8"))
        k_entries = data.get("entries", [])
    except Exception:
        return 0

    if not k_entries:
        return 0

    # Mapping: source_type -> memory_type
    type_map = {
        "extraction": "semantic",
        "chat_extraction": "semantic",
        "user_info": "semantic",
        "character_relationship": "semantic",
        "file_extraction": "semantic",
        "instagram_reaction": "episodic",
        "instagram_comment": "episodic",
    }

    memories = []
    for ke in k_entries:
        memory_type = type_map.get(ke.get("source_type", ""), "semantic")
        # Relationship entries bekommen Tag
        tags = []
        related = ke.get("related_character", "")
        if related:
            tags.append("relationship")
        source_type = ke.get("source_type", "")
        if source_type:
            tags.append(source_type)

        memories.append({
            "id": _new_id(),
            "timestamp": ke.get("timestamp", _now_iso()),
            "memory_type": memory_type,
            "content": ke.get("content", ""),
            "context": ke.get("source", ""),
            "importance": ke.get("importance", 3),
            "access_count": 0,
            "last_accessed": "",
            "decay_factor": 1.0,
            "tags": tags,
            "related_character": related,
        })

    save_memories(character_name, memories)
    logger.info("Migriert %d knowledge -> memory fuer %s", len(memories), character_name)
    return len(memories)

"""Character Knowledge System - Persistenter Wissens-Store pro Character.

Speichert kompakte Wissens-Eintraege die in den System-Prompt injiziert werden.
Characters "erinnern" sich an Interaktionen mit anderen Characters.

Storage: world.db Tabelle `knowledge` (pro Welt).
"""
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

from app.core.log import get_logger

logger = get_logger("knowledge")


def _format_knowledge_timestamp(iso_ts: str) -> str:
    """Formatiert einen ISO-Timestamp als kompaktes, LLM-lesbares Datum.

    Gibt relative Angaben fuer heute/gestern und absolute fuer aeltere zurueck.
    Beispiele: 'heute 14:30', 'gestern 09:15', '11.03. 18:00'
    """
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

from app.core.db import get_connection, transaction


def _get_max_entries() -> int:
    """Max gespeicherte Eintraege (Sliding Window)."""
    return int(os.environ.get("KNOWLEDGE_MAX_ENTRIES", "50"))


def _get_max_prompt_entries() -> int:
    """Max Eintraege im System-Prompt."""
    return int(os.environ.get("KNOWLEDGE_MAX_PROMPT_ENTRIES", "20"))


def _row_to_entry(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile in ein Knowledge-Entry-Dict.
    Schema: (id INTEGER, character_name, topic, content, tier, ts, meta)
    Der eigentliche Entry-Aufbau wird im meta-Blob gespeichert.
    """
    meta = {}
    try:
        meta = json.loads(row[6] or "{}")
    except Exception:
        pass
    # meta haelt das komplette originale Entry-Dict
    if not meta:
        # Minimaler Fallback aus den Tabellen-Feldern
        meta = {
            "id": f"k_{row[0]}",
            "timestamp": row[5] or "",
            "source_type": row[2] or "",
            "content": row[3] or "",
        }
    return meta


def load_knowledge(character_name: str) -> List[Dict[str, Any]]:
    """Laedt alle Knowledge-Eintraege eines Characters aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, character_name, topic, content, tier, ts, meta "
            "FROM knowledge WHERE character_name=? ORDER BY ts ASC",
            (character_name,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]
    except Exception as e:
        logger.error("load_knowledge DB-Fehler fuer %s: %s", character_name, e)
        return []


def save_knowledge(character_name: str, entries: List[Dict[str, Any]]):
    """Speichert Knowledge-Eintraege in die DB.

    Da die knowledge-Tabelle einen INTEGER PK (AUTOINCREMENT) hat und kein
    eigenes Conflict-Target fuer String-IDs bietet, wird der gesamte
    Character-Stand in drei Schritten aktualisiert:
    1. Alle vorhandenen Zeilen des Characters laden (id INTEGER -> entry str_id)
    2. Nicht mehr vorhandene Eintraege loeschen
    3. Neue Eintraege einfuegen / vorhandene aktualisieren (UPDATE by db_id)
    """
    max_entries = _get_max_entries()
    if len(entries) > max_entries:
        entries = entries[-max_entries:]

    try:
        with transaction() as conn:
            # Vorhandene Zeilen laden: db_id -> str_id (aus meta)
            existing_rows = conn.execute(
                "SELECT id, meta FROM knowledge WHERE character_name=?",
                (character_name,),
            ).fetchall()
            # db_id -> str_id mapping
            db_to_str: Dict[int, str] = {}
            str_to_db: Dict[str, int] = {}
            for db_id, meta_str in existing_rows:
                try:
                    m = json.loads(meta_str or "{}")
                    str_id = m.get("id", f"k_{db_id}")
                except Exception:
                    str_id = f"k_{db_id}"
                db_to_str[db_id] = str_id
                str_to_db[str_id] = db_id

            new_str_ids = {e.get("id") for e in entries if e.get("id")}

            # Geloeschte Eintraege entfernen
            for db_id, str_id in db_to_str.items():
                if str_id not in new_str_ids:
                    conn.execute(
                        "DELETE FROM knowledge WHERE id=?",
                        (db_id,),
                    )

            # Upsert fuer alle Eintraege
            for entry in entries:
                str_id = entry.get("id")
                if not str_id:
                    continue
                topic = entry.get("source_type", entry.get("topic", ""))
                content = entry.get("content", "")
                tier = entry.get("importance", entry.get("tier", ""))
                ts = entry.get("timestamp", entry.get("ts", ""))
                meta_str = json.dumps(entry, ensure_ascii=False)

                if str_id in str_to_db:
                    conn.execute(
                        "UPDATE knowledge SET topic=?, content=?, tier=?, ts=?, meta=? "
                        "WHERE id=?",
                        (str(topic), content, str(tier), ts, meta_str,
                         str_to_db[str_id]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO knowledge "
                        "(character_name, topic, content, tier, ts, meta) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (character_name, str(topic), content,
                         str(tier), ts, meta_str),
                    )
    except Exception as e:
        logger.error("save_knowledge DB-Fehler fuer %s: %s", character_name, e)


def upsert_character_relationship(character_name: str,
    related_character: str,
    new_fact: str,
    max_facts: int = 5,
    replace_prefix: str = "") -> Dict[str, Any]:
    """Aktualisiert den Beziehungs-Eintrag zu einem anderen Character.

    Pro related_character gibt es genau EINEN Eintrag (source_type="character_relationship").
    Neue Fakten werden angehaengt, alte bei Ueberlauf gekuerzt.
    Das summary-Feld wird bei neuen Fakten als stale markiert und
    periodisch per LLM neu generiert.

    replace_prefix: Wenn gesetzt, werden alle bestehenden Fakten mit diesem
    Prefix entfernt bevor der neue Fakt hinzugefuegt wird.
    So bleibt z.B. pro Beziehung nur ein Instagram-Kommentar statt vieler.
    """
    entries = load_knowledge(character_name)

    # Bestehenden Eintrag suchen
    existing = None
    for e in entries:
        if (
            e.get("source_type") == "character_relationship"
            and e.get("related_character") == related_character
        ):
            existing = e
            break

    new_fact = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', new_fact).strip()
    if not new_fact:
        return existing or {}

    if existing:
        # Bisherige Fakten parsen (zeilenweise)
        current = existing.get("content", "")
        facts = [f.strip() for f in current.split("\n") if f.strip()]

        # replace_prefix: alle Facts mit diesem Prefix entfernen
        if replace_prefix:
            facts = [f for f in facts if not f.startswith(replace_prefix)]

        # Duplikat-Check (exakt gleicher Text)
        if new_fact not in facts:
            facts.append(new_fact)

        # Aelteste kuerzen wenn zu viele
        if len(facts) > max_facts:
            facts = facts[-max_facts:]

        existing["content"] = "\n".join(facts)
        existing["timestamp"] = datetime.now().isoformat()
        existing["summary_stale"] = True  # Summary muss neu generiert werden
        save_knowledge(character_name, entries)
        logger.debug("Updated relationship %s -> %s", character_name, related_character)
        return existing
    else:
        # Neuen Eintrag erstellen
        entry = {
            "id": f"k_{uuid.uuid4().hex[:12]}",
            "timestamp": datetime.now().isoformat(),
            "source_type": "character_relationship",
            "source": f"relationship:{related_character}",
            "content": new_fact,
            "related_character": related_character,
            "importance": 2,
            "summary": "",
            "summary_stale": True,
        }
        entries.append(entry)
        save_knowledge(character_name, entries)
        logger.debug("New relationship %s -> %s", character_name, related_character)
        return entry


def build_knowledge_prompt_section(character_name: str,
    user_name: str = "") -> str:
    """Baut einen token-effizienten Knowledge-Abschnitt fuer den System-Prompt.

    Filtert: file_extraction-Eintraege ueber andere Characters (granulare
    Aktivitaeten) werden NICHT in den System-Prompt aufgenommen — sie sind
    per KnowledgeSearch abrufbar. Das haelt den Prompt fokussiert auf:
    - Eigenwissen des Characters
    - User-bezogene Fakten (aus Chat-Extraktion)
    - Beziehungs-Zusammenfassungen (character_relationship)

    Sortiert nach Importance (absteigend), dann Aktualitaet.
    Gibt leeren String zurueck wenn kein Wissen vorhanden.
    """
    entries = load_knowledge(character_name)
    if not entries:
        return ""

    max_prompt = _get_max_prompt_entries()

    # Filter: file_extraction ueber andere Characters raus (KnowledgeSearch-Territorium)
    char_lower = character_name.lower()
    filtered = []
    for e in entries:
        related = (e.get("related_character") or "").strip()
        source_type = e.get("source_type", "")
        if (
            source_type == "file_extraction"
            and related
            and related.lower() != char_lower
        ):
            continue  # Andere Character-Aktivitaeten: nur via KnowledgeSearch
        filtered.append(e)

    # Sortiere: wichtigste + neueste zuerst
    filtered.sort(
        key=lambda e: (e.get("importance", 3), e.get("timestamp", "")),
        reverse=True
    )
    filtered = filtered[:max_prompt]

    if not filtered:
        return ""

    lines = []
    for entry in filtered:
        related = f" (about {entry['related_character']})" if entry.get("related_character") else ""
        ts_label = _format_knowledge_timestamp(entry.get("timestamp", ""))
        ts_prefix = f"[{ts_label}] " if ts_label else ""
        # Fuer character_relationship: Summary statt rohe Fakten nutzen
        if entry.get("source_type") == "character_relationship" and entry.get("summary"):
            lines.append(f"- {ts_prefix}{entry['summary']}{related}")
        else:
            content = entry.get("content", "")
            # Mehrzeilige Eintraege (z.B. zusammengefuehrte file_extraction) als einzelne Bullets
            content_lines = [l.strip() for l in content.split("\n") if l.strip()]
            if len(content_lines) > 1:
                for cl in content_lines:
                    lines.append(f"- {ts_prefix}{cl}{related}")
            else:
                lines.append(f"- {ts_prefix}{content}{related}")

    header = "\nThings you know and remember"
    if user_name:
        header += f" (the user is {user_name} — other names are other characters, not the user)"
    header += ". Timestamps in brackets show when you learned this — only state what you actually know, never invent details:\n"
    return header + "\n".join(lines)



"""Chat History Management - Zusammenfassung, zeitgesteuertes Window und Tages-Summaries

Storage: world.db — Tabellen summaries, chat_messages
"""
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("history_manager")


# ---------------------------------------------------------------------------
# Memory Thresholds — zentrale Konfiguration fuer das 3-Stufen-System
# ---------------------------------------------------------------------------

def get_memory_thresholds() -> dict:
    """Drei unabhaengig konfigurierbare Grenzen fuer das Gedaechtnis-System.

    Returns dict with:
        short_term_days: Stufe 1 (Chat-History im Prompt)
        mid_term_days:   Stufe 2 → 3 Grenze (Tages → Wochen)
        long_term_days:  Wochen → Monats-Grenze
        max_messages:    Safety-Cap fuer Chat-History
    """
    return {
        "short_term_days": int(os.environ.get("MEMORY_SHORT_TERM_DAYS", "3")),
        "mid_term_days": int(os.environ.get("MEMORY_MID_TERM_DAYS", "30")),
        "long_term_days": int(os.environ.get("MEMORY_LONG_TERM_DAYS", "90")),
        "max_messages": int(os.environ.get("CHAT_HISTORY_MAX_MESSAGES", "100")),
    }


# ---------------------------------------------------------------------------
# Zeitgesteuertes History-Window
# ---------------------------------------------------------------------------

def get_time_based_history(
    full_history: List[Dict],
    days: int = 0,
    max_messages: int = 0) -> Tuple[List[Dict], List[Dict]]:
    """Gibt Chat-History der letzten N Tage zurueck.

    Filtert nach Timestamp statt nach fixer Anzahl.
    Nachrichten ohne Timestamp zaehlen als aktuell.

    Args:
        full_history: Vollstaendige Chat-History (dicts mit 'timestamp')
        days: Zeitfenster in Tagen (0 = aus Config)
        max_messages: Safety-Cap (0 = aus Config)

    Returns:
        (recent_messages, old_messages)
        recent = im Prompt, old = fuer Summary-Generierung
    """
    if not days or not max_messages:
        thresholds = get_memory_thresholds()
        days = days or thresholds["short_term_days"]
        max_messages = max_messages or thresholds["max_messages"]

    cutoff = datetime.now() - timedelta(days=days)
    recent: List[Dict] = []
    old: List[Dict] = []

    for msg in full_history:
        ts_str = msg.get("timestamp", "")
        if ts_str:
            try:
                msg_time = datetime.fromisoformat(ts_str)
                if msg_time < cutoff:
                    old.append(msg)
                    continue
            except (ValueError, TypeError):
                pass  # Kein gueltiger Timestamp → als aktuell behandeln
        recent.append(msg)

    # Safety-Cap: bei Ueberschreitung aelteste recent-Nachrichten abschneiden
    if len(recent) > max_messages:
        overflow = recent[:-max_messages]
        old.extend(overflow)
        recent = recent[-max_messages:]

    return recent, old


def _resolve_user_name() -> str:
    """Resolve player display name: active character > username > 'Player'."""
    try:
        from app.models.account import get_active_character, get_user_name
        name = get_active_character() or get_user_name()
        return name if name else "Player"
    except Exception:
        return "Player"


def _clean_message_for_summary(content: str) -> str:
    """Entfernt Tool-Calls, Bild-URLs und technische Artefakte aus einer Nachricht.

    Verhindert, dass alte Tool-Call-Patterns in die Summary gelangen und
    vom LLM als neue Tool-Calls halluziniert werden.
    """
    # Tool-Call-Patterns entfernen (alle Formate)
    # Tag-Format: <tool name="...">...</tool>
    content = re.sub(r'<tool\s+name="[^"]*">[\s\S]*?</tool>', '', content)
    # Natural EN: Use ToolName for: ...
    content = re.sub(r'(?:I\s+)?[Uu]se\s+\w+\s+for:\s*.*?(?:\n|$)', '', content)
    # Natural DE: Ich nutze ToolName für: ...
    content = re.sub(r'(?:Ich\s+)?[Nn]utze\s+\w+\s+f(?:ü|ue)r:\s*.*?(?:\n|$)', '', content)

    # Markdown-Bilder entfernen: ![...](...)
    content = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', content)
    # Rohe Bild-URLs entfernen
    content = re.sub(r'/(?:characters|instagram)/\S+\.png\S*', '', content)

    # Technische Artefakte entfernen
    content = re.sub(r'Post-ID:\s*\S+', '', content)
    content = re.sub(r'Fehler:.*?(?:\n|$)', '', content)

    # LLM-Tokenizer-Artefakte entfernen (z.B. <SPECIAL_28>, <|END_OF_TURN_TOKEN|>)
    content = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', content)

    # Mehrfache Leerzeilen zusammenfassen
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


def _create_history_summary(
    old_history: List[Dict[str, str]],
    character_name: str = "") -> str:
    """Erstellt eine Zusammenfassung aelterer Chat-Nachrichten via Router (Task: consolidation).

    Args:
        old_history: Aeltere Chat-Nachrichten, die zusammengefasst werden sollen
        character_name: Character-Name (fuer Logging)
    """
    if not old_history:
        return ""

    # Resolve user display name
    user_display_name = _resolve_user_name()

    # Nachrichten bereinigen (Tool-Calls, Bild-URLs etc. entfernen)
    cleaned_parts = []
    for msg in old_history:
        role = user_display_name if msg['role'] == 'user' else (character_name or 'Assistant')
        cleaned = _clean_message_for_summary(msg['content'])
        if cleaned:
            cleaned_parts.append(f"{role}: {cleaned}")

    if not cleaned_parts:
        return ""

    # Nachrichten begrenzen um extrem lange Prompts zu vermeiden
    max_parts = 60
    if len(cleaned_parts) > max_parts:
        cleaned_parts = cleaned_parts[-max_parts:]

    history_text = "\n".join(cleaned_parts)

    # Textlaenge hart begrenzen (ca. 8000 Tokens)
    max_chars = 24000
    if len(history_text) > max_chars:
        history_text = history_text[-max_chars:]

    # Sprache des Characters ermitteln
    lang_instruction = ""
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) if character_name else {}
        lang_code = profile.get("language", "")
        if lang_code and lang_code != "en":
            from app.models.character import LANGUAGE_MAP
            lang_name = LANGUAGE_MAP.get(lang_code, lang_code)
            lang_instruction = f"\nWrite the summary in {lang_name}."
    except Exception:
        pass

    # Context line for the prompt
    context_line = ""
    if character_name and user_display_name != "Player":
        context_line = f"This is a conversation between {user_display_name} (user) and {character_name} (character).\n\n"

    from app.core.prompt_templates import render_task

    def _build(text: str) -> tuple:
        return render_task(
            "consolidation_history_summary",
            user_display_name=user_display_name,
            character_name=character_name or "Assistant",
            context_line=context_line,
            lang_instruction=lang_instruction,
            history_text=text)

    sys_prompt, summary_prompt = _build(history_text)

    try:
        from app.core.llm_router import llm_call
        response = llm_call(
            task="consolidation",
            system_prompt=sys_prompt,
            user_prompt=summary_prompt,
            agent_name=character_name)
        summary = (response.content or "").strip()

        # Sicherheitsnetz: Tool-Call-Patterns auch aus der Summary entfernen
        summary = _clean_message_for_summary(summary)

        return summary
    except Exception as e:
        err_str = str(e)
        # Context-Size Error: Prompt kuerzen und erneut versuchen
        if "exceed" in err_str and "context" in err_str:
            logger.warning("Context-Size Fehler, kuerze Prompt und retry...")
            shorter = "\n".join(cleaned_parts[-30:])
            if len(shorter) > 12000:
                shorter = shorter[-12000:]
            _, retry_prompt = _build(shorter)
            try:
                response = llm_call(
                    task="consolidation",
                    system_prompt=sys_prompt,
                    user_prompt=retry_prompt,
                    agent_name=character_name)
                summary = _clean_message_for_summary((response.content or "").strip())
                return summary
            except Exception as retry_e:
                logger.error("Summary retry auch fehlgeschlagen: %s", retry_e)
        else:
            logger.error("Summary creation error: %s", e)
        return ""


# === Cached Summary (non-blocking) ===

def get_cached_summary(character_name: str) -> str:
    """Laedt gecachte History-Summary aus DB. Fallback auf JSON-Datei."""
    try:
        conn = get_connection()
        row = conn.execute("""
            SELECT content FROM summaries
            WHERE character_name=? AND kind='history' AND date_key='current'
        """, (character_name,)).fetchone()
        if row:
            return row[0] or ""
    except Exception as e:
        logger.debug("get_cached_summary DB-Fehler fuer %s: %s", character_name, e)

    # Fallback: JSON-Datei
    from app.models.character import get_character_dir
    path = get_character_dir(character_name) / "history_summary.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("summary", "")
        except Exception:
            pass
    return ""


def _save_cached_summary(character_name: str, summary: str, message_count: int = 0):
    """Speichert eine History-Summary in der DB."""
    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO summaries (character_name, kind, date_key, content, meta)
                VALUES (?, 'history', 'current', ?, ?)
                ON CONFLICT(character_name, kind, date_key) DO UPDATE SET
                    content=excluded.content,
                    meta=excluded.meta
            """, (
                character_name,
                summary,
                json.dumps({"message_count": message_count, "updated_at": now},
                           ensure_ascii=False),
            ))
    except Exception as e:
        logger.error("_save_cached_summary DB-Fehler fuer %s: %s", character_name, e)


_SUMMARY_THROTTLE_MINUTES = 30  # Mindestabstand zwischen Summary-Updates


def _is_summary_fresh(character_name: str) -> bool:
    """Prueft ob die gecachte Summary juenger als THROTTLE_MINUTES ist."""
    try:
        conn = get_connection()
        row = conn.execute("""
            SELECT meta FROM summaries
            WHERE character_name=? AND kind='history' AND date_key='current'
        """, (character_name,)).fetchone()
        if row:
            meta = json.loads(row[0] or "{}")
            updated = meta.get("updated_at", "")
            if updated:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                return age < _SUMMARY_THROTTLE_MINUTES * 60
    except Exception:
        pass
    return False


def update_summary_background(character_name: str, old_messages: List[Dict[str, str]]):
    """Aktualisiert die Sitzungs-Summary (Chat vor Sliding Window).

    Wird nach dem Chat aufgerufen, blockiert den Chat NICHT.
    Throttled: Maximal alle 30 Minuten ein Update.

    NUR die History-Summary — Tages-Summaries werden in der
    Konsolidierungs-Pipeline erstellt (alle 6h), nicht im Chat-Path.
    """
    if _is_summary_fresh(character_name):
        logger.debug("Summary fuer %s noch frisch, ueberspringe Update", character_name)
        return

    try:
        summary = _create_history_summary(old_messages, character_name=character_name)
        if summary:
            _save_cached_summary(character_name, summary, len(old_messages))
            logger.info("Session-Summary aktualisiert fuer %s (%d Nachrichten)", character_name, len(old_messages))
    except Exception as e:
        logger.error("Session-Summary fehlgeschlagen: %s", e)


# === Daily Summaries ===

def load_daily_summaries(character_name: str) -> Dict[str, str]:
    """Laedt alle Tages-Summaries aus DB. Returns {date_str: summary_text}."""
    try:
        conn = get_connection()
        rows = conn.execute("""
            SELECT date_key, content FROM summaries
            WHERE character_name=? AND kind='daily'
            ORDER BY date_key ASC
        """, (character_name,)).fetchall()
        if rows:
            return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.debug("load_daily_summaries DB-Fehler fuer %s: %s", character_name, e)

    # Fallback: JSON-Datei
    from app.models.character import get_character_dir
    path = get_character_dir(character_name) / "daily_summaries.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("summaries", {})
        except Exception:
            pass
    return {}


def save_daily_summary(character_name: str, date_str: str, summary: str):
    """Speichert/ueberschreibt eine Tages-Summary in der DB."""
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO summaries (character_name, kind, date_key, content)
                VALUES (?, 'daily', ?, ?)
                ON CONFLICT(character_name, kind, date_key) DO UPDATE SET
                    content=excluded.content
            """, (character_name, date_str, summary))
    except Exception as e:
        logger.error("save_daily_summary DB-Fehler fuer %s: %s", character_name, e)


def delete_daily_summaries(character_name: str, date_keys: List[str]):
    """Loescht Tages-Summaries aus der DB."""
    if not date_keys:
        return
    try:
        with transaction() as conn:
            placeholders = ",".join("?" for _ in date_keys)
            conn.execute(
                f"DELETE FROM summaries WHERE character_name=? AND kind='daily' "
                f"AND date_key IN ({placeholders})",
                (character_name, *date_keys),
            )
    except Exception as e:
        logger.error("delete_daily_summaries DB-Fehler fuer %s: %s", character_name, e)


def get_recent_daily_summaries(character_name: str, days: int = 0) -> List[Dict[str, str]]:
    """Gibt die letzten N Tage mit Summaries zurueck (aelteste zuerst).

    Returns: [{"date": "2026-02-23", "summary": "..."}, ...]
    """
    if days <= 0:
        days = int(os.environ.get("DAILY_SUMMARY_DAYS", "7"))

    summaries = load_daily_summaries(character_name)
    if not summaries:
        return []

    today = date.today()
    result = []
    for i in range(days, 0, -1):
        day = today - timedelta(days=i)
        day_str = day.isoformat()
        if day_str in summaries:
            result.append({"date": day_str, "summary": summaries[day_str]})

    return result


def build_daily_summary_prompt_section(character_name: str,
                                       max_days: int = 0) -> str:
    """Baut den Prompt-Abschnitt fuer Tages-Summaries (Stufe 2: SHORT bis MID).

    Laedt Tages-Summaries ab SHORT_TERM_DAYS (Default 3) bis max_days.
    Tage innerhalb SHORT_TERM_DAYS werden uebersprungen — die Chat-History
    deckt diese bereits ab.

    max_days: 0 = MID_TERM_DAYS (Default 30). Kann reduziert werden
              fuer TalkTo/Social (z.B. 7 Tage).

    Format:
    Recent days:
    - Feb 23: Summary text...
    - Feb 24: Summary text...
    """
    thresholds = get_memory_thresholds()
    if max_days <= 0:
        max_days = thresholds["mid_term_days"]
    short = thresholds["short_term_days"]

    recent = get_recent_daily_summaries(character_name, days=max_days)
    if not recent:
        return ""

    # Nur Tage aelter als SHORT_TERM_DAYS (Chat-History deckt die Kurzzeit ab)
    cutoff = date.today() - timedelta(days=short)
    lines = []
    for entry in recent:
        try:
            d = date.fromisoformat(entry["date"])
            if d > cutoff:
                continue  # Innerhalb Kurzzeit — Chat-History reicht
            label = d.strftime("%b %d")
        except ValueError:
            label = entry["date"]
        lines.append(f"- {label}: {entry['summary']}")

    if not lines:
        return ""
    return "\nRecent days:\n" + "\n".join(lines)


def build_longterm_summary_prompt_section(character_name: str) -> str:
    """Baut den Prompt-Abschnitt fuer Langzeit-Gedaechtnis (Stufe 3: Wochen + Monate).

    Format:
    Long-term memories:
    Months:
    - 2026-01: Summary...
    Weeks:
    - 2026-W10: Summary...
    """
    from app.core.memory_service import load_monthly_summaries, load_weekly_summaries

    monthly = load_monthly_summaries(character_name)
    weekly = load_weekly_summaries(character_name)

    if not monthly and not weekly:
        return ""

    parts = ["\nLong-term memories:"]

    if monthly:
        parts.append("Months:")
        for month_key in sorted(monthly.keys()):
            parts.append(f"- {month_key}: {monthly[month_key]}")

    if weekly:
        parts.append("Weeks:")
        for week_key in sorted(weekly.keys()):
            parts.append(f"- {week_key}: {weekly[week_key]}")

    return "\n".join(parts)


def _get_today_messages(character_name: str) -> List[Dict[str, str]]:
    """Laedt nur die Nachrichten von heute aus der DB."""
    return _get_day_messages(character_name, date.today())


def _get_day_messages(character_name: str, day: date) -> List[Dict[str, str]]:
    """Laedt Nachrichten fuer einen bestimmten Tag aus der DB."""
    day_str = day.isoformat()
    try:
        conn = get_connection()
        rows = conn.execute("""
            SELECT role, content FROM chat_messages
            WHERE character_name=?
              AND ts >= ? AND ts < ?
            ORDER BY ts ASC
        """, (character_name, f"{day_str}T00:00:00", f"{day_str}T23:59:59")).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows if r[0] and r[1]]
    except Exception as e:
        logger.debug("_get_day_messages DB-Fehler fuer %s/%s: %s", character_name, day_str, e)
        return []


def _create_daily_summary(messages: List[Dict[str, str]],
                          character_name: str = "") -> str:
    """Erstellt eine Tages-Summary fuer gegebene Nachrichten via Router (consolidation)."""
    if not messages:
        return ""

    # Resolve user display name
    user_display_name = _resolve_user_name()

    cleaned_parts = []
    for msg in messages:
        role = user_display_name if msg["role"] == "user" else (character_name or "Assistant")
        cleaned = _clean_message_for_summary(msg["content"])
        if cleaned:
            cleaned_parts.append(f"{role}: {cleaned}")

    if not cleaned_parts:
        return ""

    # Nachrichten begrenzen
    max_parts = 80
    if len(cleaned_parts) > max_parts:
        cleaned_parts = cleaned_parts[-max_parts:]

    history_text = "\n".join(cleaned_parts)

    # Textlaenge hart begrenzen (ca. 8000 Tokens)
    max_chars = 24000
    if len(history_text) > max_chars:
        history_text = history_text[-max_chars:]

    # Sprache des Characters ermitteln
    lang_instruction = ""
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) if character_name else {}
        lang_code = profile.get("language", "")
        if lang_code and lang_code != "en":
            from app.models.character import LANGUAGE_MAP
            lang_name = LANGUAGE_MAP.get(lang_code, lang_code)
            lang_instruction = f"\nWrite the summary in {lang_name}."
    except Exception:
        pass

    # Context line
    context_line = ""
    if character_name and user_display_name != "Player":
        context_line = f"This is a conversation between {user_display_name} and {character_name}.\n\n"

    from app.core.prompt_templates import render_task
    sys_prompt, summary_prompt = render_task(
        "consolidation_today",
        user_display_name=user_display_name,
        character_name=character_name or "the character",
        context_line=context_line,
        lang_instruction=lang_instruction,
        history_text=history_text)

    try:
        from app.core.llm_router import llm_call
        response = llm_call(
            task="consolidation",
            system_prompt=sys_prompt,
            user_prompt=summary_prompt,
            agent_name=character_name)
        summary = (response.content or "").strip()
        summary = _clean_message_for_summary(summary)
        return summary
    except Exception as e:
        logger.error("Daily summary Erstellung fehlgeschlagen: %s", e)
        return ""


def _update_daily_summary(character_name: str):
    """Aktualisiert die Tages-Summary fuer heute."""
    today_messages = _get_today_messages(character_name)
    if len(today_messages) < 4:
        # Zu wenige Nachrichten fuer eine sinnvolle Summary
        return

    today_str = date.today().isoformat()
    summary = _create_daily_summary(today_messages, character_name=character_name)
    if summary:
        save_daily_summary(character_name, today_str, summary)
        logger.info("Daily summary %s: %s aktualisiert (%d Nachrichten)", character_name, today_str, len(today_messages))


def _is_bad_summary(summary: str) -> bool:
    """Prueft ob eine Summary offensichtlich kaputt ist und neu generiert werden sollte."""
    if not summary or len(summary) < 30:
        return True
    s = summary.lower()
    bad_patterns = [
        "it seems like you",
        "it looks like you",
        "end of extract",
        "i can't help",
        "i cannot help",
        "as an ai",
        "as a language model",
    ]
    return any(p in s for p in bad_patterns)


def backfill_missing_daily_summaries(character_name: str):
    """Erstellt fehlende Tages-Summaries fuer vergangene Tage.

    Prueft die letzten 7 Tage. Ueberspringt heute (wird separat aktualisiert)
    und Tage die bereits eine Summary haben.
    """
    from app.models.character import get_character_dir
    from app.models.account import get_user_name

    existing = load_daily_summaries(character_name)
    chat_dir = get_character_dir(character_name) / "chats"
    if not chat_dir.exists():
        return

    display_name = get_user_name()
    key_base = display_name if display_name else ""
    today = date.today()
    days = int(os.environ.get("DAILY_SUMMARY_DAYS", "7"))

    backfilled = 0
    max_backfill_per_run = 2  # Maximal 2 Tage pro Durchlauf um Queue nicht zu blockieren

    for i in range(1, days + 1):
        if backfilled >= max_backfill_per_run:
            break

        day = today - timedelta(days=i)
        day_str = day.isoformat()

        # Bereits vorhanden → skip (ausser offensichtlich kaputt)
        if day_str in existing and not _is_bad_summary(existing[day_str]):
            continue

        # Chat-Datei fuer diesen Tag?
        day_file = chat_dir / f"{key_base}_chat_{day_str}.json"
        if not day_file.exists():
            continue

        messages = _get_day_messages(character_name, day)
        if len(messages) < 4:
            continue

        summary = _create_daily_summary(messages, character_name=character_name)
        if summary:
            save_daily_summary(character_name, day_str, summary)
            logger.info("Daily summary backfill %s: %s (%d Nachrichten)", character_name, day_str, len(messages))
            backfilled += 1

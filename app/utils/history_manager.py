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
        session_gap_hours: Zeitluecke zwischen Turns, die als Session-Bruch
                           zaehlt — alles vor der Luecke wandert in old_history.
    """
    return {
        "short_term_days": int(os.environ.get("MEMORY_SHORT_TERM_DAYS", "3")),
        "mid_term_days": int(os.environ.get("MEMORY_MID_TERM_DAYS", "30")),
        "long_term_days": int(os.environ.get("MEMORY_LONG_TERM_DAYS", "90")),
        "max_messages": int(os.environ.get("CHAT_HISTORY_MAX_MESSAGES", "100")),
        "session_gap_hours": float(os.environ.get("CHAT_SESSION_GAP_HOURS", "4")),
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

    Zusaetzlich: Session-Gap. Wenn zwischen zwei aufeinanderfolgenden
    recent-Turns eine Pause groesser als ``session_gap_hours`` liegt,
    werden alle Turns VOR der letzten solchen Luecke nach ``old`` umgehaengt.
    Dadurch sieht das LLM nur die aktuelle Session verbatim — alles davor
    wandert in die Session-Summary.

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
    else:
        thresholds = get_memory_thresholds()
    gap_hours = thresholds.get("session_gap_hours", 4.0)

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

    # Session-Gap: an der letzten Luecke > gap_hours abschneiden.
    if gap_hours > 0 and len(recent) >= 2:
        gap = timedelta(hours=gap_hours)
        for i in range(len(recent) - 1, 0, -1):
            try:
                prev_ts = datetime.fromisoformat(recent[i - 1].get("timestamp") or "")
                cur_ts = datetime.fromisoformat(recent[i].get("timestamp") or "")
            except (ValueError, TypeError):
                continue
            if cur_ts - prev_ts > gap:
                old.extend(recent[:i])
                recent = recent[i:]
                break

    return recent, old


_FUZZY_MARKER_RE = re.compile(
    r'\*\*I\s+(?:feel|do|am\s+at)\s+[^*]+\*\*', re.IGNORECASE)
_FUZZY_NONALNUM_RE = re.compile(r'[^a-z0-9äöüß]+')


def fuzzy_signature(content: str, length: int = 60) -> str:
    """Erzeugt eine fuzzy-Signatur einer Assistant-Antwort fuer Repetitions-
    Erkennung. Marker (`**I feel ...**`), Whitespace, Satzzeichen werden
    rausgefiltert; die ersten ``length`` alphanumerischen Zeichen sind die
    Signatur. So matchen Antworten die nur in Marker/Whitespace/Akzent
    abweichen.
    """
    if not content:
        return ""
    s = _FUZZY_MARKER_RE.sub("", content).lower()
    s = _FUZZY_NONALNUM_RE.sub("", s)
    return s[:length]


def detect_assistant_repetition(messages: List[Dict[str, str]],
                                 lookback: int = 6) -> bool:
    """Boolean-Wrapper fuer ``count_assistant_repetitions`` — kompatibel mit
    aelteren Aufrufern. True wenn mindestens 1 Duplikat gefunden wurde.
    """
    return count_assistant_repetitions(messages, lookback) > 0


def count_assistant_repetitions(messages: List[Dict[str, str]],
                                 lookback: int = 6) -> int:
    """Zaehlt wie viele der letzten ``lookback`` Assistant-Antworten
    eine bereits gesehene Fuzzy-Signatur haben.

    Beispiel: 4 Antworten mit Signatur A + 1 mit Signatur B + 1 mit C
    → 3 Duplikate (3x A wiederholt). Ergebnis nutzbar fuer eine graduelle
    Temperature-Erhoehung (z.B. base + step * count).
    """
    if not messages:
        return 0
    sigs: List[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        sig = fuzzy_signature(msg.get("content", ""))
        if sig:
            sigs.append(sig)
        if len(sigs) >= lookback:
            break
    return len(sigs) - len(set(sigs))


def strip_history_artifacts(content: str) -> str:
    """Entfernt Artefakte aus Chat-Content bevor er ins messages-Array geht.

    Stoer-Faktoren fuer das LLM, die nicht in die Recent-History gehoeren:
    - Markdown-Bild-Embeds ``![alt](url)`` — der LLM sieht sie als kryptische
      URL-Bloecke und imitiert sie ggf. in eigenen Antworten
    - Generated-Image-Marker / Image-URL-Reste

    DB-Inhalt bleibt unveraendert; nur der vorbei-an-LLM-Stream wird saeubert.
    """
    if not content:
        return content
    # Markdown-Bild-Embeds in einer oder mehrerer Zeilen
    content = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', content)
    # Mehrfache Leerzeilen normalisieren
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def resolve_speaker(msg: Dict[str, str],
                    partner: str,
                    character_name: str) -> str:
    """Bestimmt den tatsaechlichen Sprecher einer chat_messages-Zeile.

    Priorisiert:
      1. metadata.speaker  (gesetzt bei TalkTo, Group, Inbox)
      2. partner           (bei role='user' = Gegenueber sprach)
      3. character_name    (bei role='assistant' = Selbst sprach)

    Gibt einen Charakternamen zurueck. Kein "Player"-Fallback.
    Leerstring nur, wenn weder partner noch character_name bekannt sind —
    Aufrufer sollen solche Zeilen ueberspringen.
    """
    meta = msg.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    speaker = (meta.get("speaker") or "").strip() if isinstance(meta, dict) else ""
    if speaker:
        return speaker

    role = (msg.get("role") or "").strip().lower()
    if role == "user":
        return (partner or "").strip()
    if role == "assistant":
        return (character_name or "").strip()
    return (character_name or partner or "").strip()


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
    character_name: str = "",
    partner_name: str = "") -> str:
    """Erstellt eine Zusammenfassung aelterer Chat-Nachrichten zwischen ZWEI
    Charakteren via Router (Task: consolidation).

    Args:
        old_history: Aeltere Chat-Nachrichten dieser dyadischen Konversation.
                     Kann Felder `partner`, `metadata` enthalten — werden zur
                     Sprecher-Aufloesung benutzt.
        character_name: Memory-Owner (Speaker B)
        partner_name:   Konversationspartner (Speaker A). Wenn leer, wird er
                        aus dem ersten Message-Eintrag mit role='user'
                        abgeleitet.
    """
    if not old_history:
        return ""

    if not partner_name:
        for msg in old_history:
            p = (msg.get("partner") or "").strip() if isinstance(msg, dict) else ""
            if p:
                partner_name = p
                break

    if not partner_name or not character_name:
        return ""

    # Nachrichten bereinigen (Tool-Calls, Bild-URLs etc. entfernen).
    # Sprecher pro Zeile aus metadata/partner/role aufloesen — kein
    # globales user_display-Label mehr.
    cleaned_parts = []
    for msg in old_history:
        speaker = resolve_speaker(msg, partner_name, character_name)
        if not speaker:
            continue
        cleaned = _clean_message_for_summary(msg.get('content', ''))
        if cleaned:
            cleaned_parts.append(f"{speaker}: {cleaned}")

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

    from app.core.prompt_templates import render_task

    def _build(text: str) -> tuple:
        return render_task(
            "consolidation_history_summary",
            speaker_a=partner_name,
            speaker_b=character_name,
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
                INSERT INTO summaries (character_name, kind, date_key, partner, content, meta)
                VALUES (?, 'history', 'current', '', ?, ?)
                ON CONFLICT(character_name, kind, date_key, partner) DO UPDATE SET
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


def update_summary_background(character_name: str,
                              old_messages: List[Dict[str, str]],
                              partner_name: str = ""):
    """Aktualisiert die Sitzungs-Summary (Chat vor Sliding Window).

    Wird nach dem Chat aufgerufen, blockiert den Chat NICHT.
    Throttled: Maximal alle 30 Minuten ein Update.

    NUR die History-Summary — Tages-Summaries werden in der
    Konsolidierungs-Pipeline erstellt (alle 6h), nicht im Chat-Path.

    `partner_name` ist optional — wenn leer, wird der erste Partner aus
    den Messages abgeleitet.
    """
    if _is_summary_fresh(character_name):
        logger.debug("Summary fuer %s noch frisch, ueberspringe Update", character_name)
        return

    try:
        summary = _create_history_summary(old_messages,
                                          character_name=character_name,
                                          partner_name=partner_name)
        if summary:
            _save_cached_summary(character_name, summary, len(old_messages))
            logger.info("Session-Summary aktualisiert fuer %s (%d Nachrichten)", character_name, len(old_messages))
    except Exception as e:
        logger.error("Session-Summary fehlgeschlagen: %s", e)


def _summary_updated_at(character_name: str) -> "datetime | None":
    """Liest updated_at-Timestamp der gecachten Summary."""
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
                return datetime.fromisoformat(updated)
    except Exception:
        pass
    return None


def refresh_summary_if_uncovered(
    character_name: str,
    old_messages: List[Dict[str, str]]) -> str:
    """Synchroner Refresh — gibt Summary zurueck, regeneriert wenn alte
    Cache-Summary den aktuellen ``old_messages``-Stand nicht abdeckt.

    Wird vor dem Bau des System-Prompts aufgerufen. Erkennt: enthaelt
    ``old_messages`` Eintraege juenger als ``updated_at`` der gecachten
    Summary? Falls ja → die Summary ist stale (z.B. nach Session-Gap-Cut),
    regen via LLM (~5-15s). Throttle wird hier bewusst ignoriert.

    Returns: aktuelle (ggf. neu generierte) Summary.
    """
    cached = get_cached_summary(character_name)
    if not old_messages:
        return cached

    updated_at = _summary_updated_at(character_name)

    # Juengsten Timestamp in old_messages finden
    newest_old = None
    for msg in old_messages:
        ts = msg.get("timestamp", "")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts)
            if newest_old is None or t > newest_old:
                newest_old = t
        except (ValueError, TypeError):
            continue

    needs_refresh = (
        cached == ""
        or updated_at is None
        or (newest_old is not None and newest_old > updated_at)
    )
    if not needs_refresh:
        return cached

    try:
        summary = _create_history_summary(old_messages,
                                          character_name=character_name)
        if summary:
            _save_cached_summary(character_name, summary, len(old_messages))
            logger.info(
                "Session-Summary synchron refreshed fuer %s (%d Nachrichten)",
                character_name, len(old_messages))
            return summary
    except Exception as e:
        logger.error("Sync-Summary-Refresh fehlgeschlagen: %s", e)

    return cached


# === Daily Summaries ===

def load_daily_summaries(character_name: str,
                         partner: str = "") -> Dict[str, Dict[str, str]]:
    """Laedt alle Tages-Summaries aus DB.

    Returns:
        Wenn `partner` leer: {date_str: {partner_name: summary_text, ...}}
        Wenn `partner` gesetzt: {date_str: summary_text} (Legacy-Form fuer
        einzelne Partner-Filter)
    """
    try:
        conn = get_connection()
        if partner:
            rows = conn.execute("""
                SELECT date_key, content FROM summaries
                WHERE character_name=? AND kind='daily' AND partner=?
                ORDER BY date_key ASC
            """, (character_name, partner)).fetchall()
            return {r[0]: r[1] for r in rows} if rows else {}
        rows = conn.execute("""
            SELECT date_key, partner, content FROM summaries
            WHERE character_name=? AND kind='daily'
            ORDER BY date_key ASC
        """, (character_name,)).fetchall()
        result: Dict[str, Dict[str, str]] = {}
        for date_key, p, content in rows:
            result.setdefault(date_key, {})[p or ""] = content
        return result
    except Exception as e:
        logger.debug("load_daily_summaries DB-Fehler fuer %s: %s", character_name, e)
        return {}


def load_daily_summaries_combined(character_name: str) -> Dict[str, str]:
    """Variante von load_daily_summaries fuer Konsumenten, die nicht
    partner-aware sind (Wochen-/Monats-Konsolidierung).

    Faltet pro Tag alle Partner-Eintraege in einen kombinierten String:
        "Mit Kahiro: ...
         Mit Rosi: ...
         <Allgemein>: ..."
    """
    by_day = load_daily_summaries(character_name)  # {date: {partner: text}}
    flat: Dict[str, str] = {}
    for day_str, by_partner in by_day.items():
        if not by_partner:
            continue
        parts = []
        for partner, text in by_partner.items():
            if not text:
                continue
            if partner:
                parts.append(f"Mit {partner}: {text}")
            else:
                parts.append(text)
        if parts:
            flat[day_str] = "\n".join(parts)
    return flat


def save_daily_summary(character_name: str, date_str: str, summary: str,
                       partner: str = ""):
    """Speichert/ueberschreibt eine Tages-Summary in der DB.

    `partner` ist der Konversationspartner (Charaktername). Pflicht fuer
    neue Schreibwege — Aufrufer ohne Partner schreiben in den
    Legacy-Slot ('').
    """
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO summaries (character_name, kind, date_key, partner, content)
                VALUES (?, 'daily', ?, ?, ?)
                ON CONFLICT(character_name, kind, date_key, partner) DO UPDATE SET
                    content=excluded.content
            """, (character_name, date_str, partner, summary))
    except Exception as e:
        logger.error("save_daily_summary DB-Fehler fuer %s/%s: %s",
                     character_name, partner, e)


def delete_daily_summaries(character_name: str, date_keys: List[str],
                           partner: str = ""):
    """Loescht Tages-Summaries aus der DB.

    Wenn `partner` gesetzt: nur Eintraege fuer diesen Partner. Sonst alle
    Partner-Eintraege fuer die Date-Keys.
    """
    if not date_keys:
        return
    try:
        with transaction() as conn:
            placeholders = ",".join("?" for _ in date_keys)
            if partner:
                conn.execute(
                    f"DELETE FROM summaries WHERE character_name=? AND "
                    f"kind='daily' AND partner=? AND "
                    f"date_key IN ({placeholders})",
                    (character_name, partner, *date_keys),
                )
            else:
                conn.execute(
                    f"DELETE FROM summaries WHERE character_name=? AND "
                    f"kind='daily' AND date_key IN ({placeholders})",
                    (character_name, *date_keys),
                )
    except Exception as e:
        logger.error("delete_daily_summaries DB-Fehler fuer %s: %s", character_name, e)


def get_recent_daily_summaries(character_name: str,
                               days: int = 0,
                               partner: str = "") -> List[Dict[str, str]]:
    """Gibt die letzten N Tage mit Summaries zurueck (aelteste zuerst).

    Returns: [{"date": "2026-02-23", "partner": "Kahiro", "summary": "..."}, ...]
    Wenn `partner` gesetzt: nur Summaries dieses Partners.
    """
    if days <= 0:
        days = int(os.environ.get("DAILY_SUMMARY_DAYS", "7"))

    summaries = load_daily_summaries(character_name)  # date -> {partner: text}
    if not summaries:
        return []

    today = date.today()
    result: List[Dict[str, str]] = []
    for i in range(days, 0, -1):
        day = today - timedelta(days=i)
        day_str = day.isoformat()
        per_partner = summaries.get(day_str) or {}
        if not per_partner:
            continue
        for p, text in per_partner.items():
            if partner and p != partner:
                continue
            result.append({"date": day_str, "partner": p, "summary": text})
    return result


def build_daily_summary_prompt_section(character_name: str,
                                       max_days: int = 0,
                                       partner: str = "") -> str:
    """Baut den Prompt-Abschnitt fuer Tages-Summaries (Stufe 2: SHORT bis MID).

    Laedt Tages-Summaries ab SHORT_TERM_DAYS (Default 3) bis max_days.
    Tage innerhalb SHORT_TERM_DAYS werden uebersprungen — die Chat-History
    deckt diese bereits ab.

    max_days: 0 = MID_TERM_DAYS (Default 30). Kann reduziert werden
              fuer TalkTo/Social (z.B. 7 Tage).
    partner: Wenn gesetzt, nur Summaries fuer diesen Konversationspartner.
             Sonst werden Summaries pro Tag/Partner gerendert.

    Format:
    Recent days:
    - Feb 23 with Kahiro: Summary text...
    - Feb 23 with Rosi:   Summary text...
    - Feb 24 with Kahiro: Summary text...
    """
    thresholds = get_memory_thresholds()
    if max_days <= 0:
        max_days = thresholds["mid_term_days"]
    short = thresholds["short_term_days"]

    recent = get_recent_daily_summaries(character_name, days=max_days,
                                        partner=partner)
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
        partner_label = entry.get("partner") or ""
        if partner_label:
            lines.append(f"- {label} with {partner_label}: {entry['summary']}")
        else:
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
    """Laedt Nachrichten fuer einen bestimmten Tag aus der DB.

    Liefert pro Zeile: role, content, partner, metadata, ts.
    Aufrufer entscheiden, wie sie nach Partner gruppieren oder filtern.
    """
    day_str = day.isoformat()
    try:
        conn = get_connection()
        rows = conn.execute("""
            SELECT role, content, partner, metadata, ts FROM chat_messages
            WHERE character_name=?
              AND ts >= ? AND ts < ?
            ORDER BY ts ASC
        """, (character_name, f"{day_str}T00:00:00", f"{day_str}T23:59:59")).fetchall()
        result = []
        for r in rows:
            role, content, partner, metadata, ts = r
            if not role or not content:
                continue
            result.append({
                "role": role,
                "content": content,
                "partner": partner or "",
                "metadata": metadata or "{}",
                "ts": ts or "",
            })
        return result
    except Exception as e:
        logger.debug("_get_day_messages DB-Fehler fuer %s/%s: %s", character_name, day_str, e)
        return []


def _group_by_partner(messages: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Gruppiert Tagesnachrichten nach Partner-Charakter.

    Partner ist der tatsaechliche Konversationspartner. Group-Chat-Zeilen
    (mehrere Sprecher) landen unter dem `partner`-Feld der Zeile.
    Zeilen ohne Partner werden geskipped.
    """
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for msg in messages:
        partner = (msg.get("partner") or "").strip()
        if not partner:
            continue
        grouped.setdefault(partner, []).append(msg)
    return grouped


def _create_daily_summary(messages: List[Dict[str, str]],
                          character_name: str = "",
                          partner_name: str = "") -> str:
    """Erstellt eine Tages-Summary fuer einen Konversationspartner.

    Args:
        messages: Tagesnachrichten ZWISCHEN character_name und partner_name.
                  Aufrufer hat bereits per `_group_by_partner` gefiltert.
        character_name: Memory-Owner (Speaker B)
        partner_name:   Konversationspartner (Speaker A). Pflichtfeld — ohne
                        Partner gibt es keine dyadische Summary.
    """
    if not messages:
        return ""
    if not partner_name or not character_name:
        logger.debug("daily_summary skip: partner_name=%r char=%r",
                     partner_name, character_name)
        return ""

    # Sprecher pro Zeile aufloesen — KEIN globales user_display-Label.
    cleaned_parts = []
    for msg in messages:
        speaker = resolve_speaker(msg, partner_name, character_name)
        if not speaker:
            continue
        cleaned = _clean_message_for_summary(msg.get("content", ""))
        if cleaned:
            cleaned_parts.append(f"{speaker}: {cleaned}")

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

    from app.core.prompt_templates import render_task
    sys_prompt, summary_prompt = render_task(
        "consolidation_today",
        speaker_a=partner_name,
        speaker_b=character_name,
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
    """Aktualisiert die Tages-Summary fuer heute — pro Konversationspartner
    eine eigene Summary."""
    today_messages = _get_today_messages(character_name)
    if not today_messages:
        return

    grouped = _group_by_partner(today_messages)
    today_str = date.today().isoformat()
    for partner, msgs in grouped.items():
        if len(msgs) < 4:
            # Zu wenige Nachrichten fuer eine sinnvolle Summary
            continue
        summary = _create_daily_summary(msgs,
                                        character_name=character_name,
                                        partner_name=partner)
        if summary:
            save_daily_summary(character_name, today_str, summary,
                               partner=partner)
            logger.info("Daily summary %s↔%s %s: aktualisiert (%d Nachrichten)",
                        character_name, partner, today_str, len(msgs))


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
    """Erstellt fehlende Tages-Summaries fuer vergangene Tage — pro Partner.

    Prueft die letzten 7 Tage. Ueberspringt heute (wird separat aktualisiert)
    und (Tag, Partner)-Paare die bereits eine Summary haben.
    """
    existing = load_daily_summaries(character_name)  # {date: {partner: text}}
    today = date.today()
    days = int(os.environ.get("DAILY_SUMMARY_DAYS", "7"))

    backfilled = 0
    max_backfill_per_run = 2  # Cap pro Durchlauf, damit die Queue nicht blockt

    for i in range(1, days + 1):
        if backfilled >= max_backfill_per_run:
            break

        day = today - timedelta(days=i)
        day_str = day.isoformat()

        messages = _get_day_messages(character_name, day)
        if not messages:
            continue

        grouped = _group_by_partner(messages)
        if not grouped:
            continue

        existing_for_day = existing.get(day_str) or {}

        for partner, msgs in grouped.items():
            if backfilled >= max_backfill_per_run:
                break
            if len(msgs) < 4:
                continue
            # Bereits vorhanden und sauber → skip
            if (partner in existing_for_day
                    and not _is_bad_summary(existing_for_day[partner])):
                continue

            summary = _create_daily_summary(msgs,
                                            character_name=character_name,
                                            partner_name=partner)
            if summary:
                save_daily_summary(character_name, day_str, summary,
                                   partner=partner)
                logger.info("Daily summary backfill %s↔%s %s (%d Nachrichten)",
                            character_name, partner, day_str, len(msgs))
                backfilled += 1

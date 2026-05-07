"""Memory Service - Extraction, Consolidation und Background-Worker.

Extrahiert Memories aus Chat-Austausch (beide Seiten) und
konsolidiert aeltere Memories periodisch.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("memory_service")


# ---------------------------------------------------------------------------
# Memory Extraction (aus Chat-Austausch)
# ---------------------------------------------------------------------------

def extract_memories_from_exchange(character_name: str,
    partner_name: str,
    partner_message: str,
    own_message: str,
    llm) -> List[Dict[str, Any]]:
    """Extrahiert Memories aus einem Charakter-zu-Charakter-Austausch.

    Args:
        character_name: Memory-Owner (Speaker B im Template)
        partner_name:   Konversationspartner (Speaker A im Template) — z.B.
                        ein Avatar-Name oder ein anderer NPC-Name. Wenn leer,
                        wird die Extraktion uebersprungen.
        partner_message: Was der Partner sagte (text_a)
        own_message:    Was der Memory-Owner sagte (text_b)

    Returns list of extracted memory dicts.
    """
    from app.models.character import get_character_config
    config = get_character_config(character_name)

    if not config.get("extraction_enabled", True):
        return []

    # Ohne Partner kein dyadischer Austausch → nichts zu extrahieren.
    partner_name = (partner_name or "").strip()
    if not partner_name:
        logger.debug("[%s] extraction skipped: no partner_name", character_name)
        return []

    # Bestehende Memories fuer Deduplizierung + Commitment-Tracking
    from app.models.memory import load_memories
    existing = load_memories(character_name)
    existing_summary = "\n".join(
        f"- {e['content']}" for e in existing[-15:]
    ) if existing else "Noch keine Erinnerungen."

    # Offene Commitments auflisten (fuer Completion-Erkennung)
    open_commitments = [
        e for e in existing
        if e.get("memory_type") == "commitment"
        and "completed" not in e.get("tags", [])
    ]
    commitments_list = "\n".join(
        f"- [ID:{c['id']}] {c['content']}" for c in open_commitments[-10:]
    ) if open_commitments else ""

    # Clean own message (remove meta-tags)
    clean_own = re.sub(r'\*\*I\s+feel\s+[^*]+\*\*', '', own_message, flags=re.IGNORECASE)
    clean_own = re.sub(r'\*\*I\s+am\s+at\s+[^*]+\*\*', '', clean_own, flags=re.IGNORECASE)
    clean_own = re.sub(r'\*\*I\s+do\s+[^*]+\*\*', '', clean_own, flags=re.IGNORECASE)
    clean_own = re.sub(r'\[INTENT:[^\]]+\]', '', clean_own)
    clean_own = clean_own.strip()

    commitments_block = (
        "Open promises/plans (check if any was fulfilled by this exchange):\n"
        + commitments_list
    ) if commitments_list else ""

    try:
        from app.core.llm_router import llm_call
        from app.core.prompt_templates import render_task

        sys_prompt, user_prompt = render_task(
            "extraction_memory",
            speaker_a=partner_name,
            speaker_b=character_name,
            text_a=partner_message,
            text_b=clean_own[:1500],
            existing_summary=existing_summary,
            commitments_block=commitments_block)

        response = llm_call(
            task="extraction",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name=character_name)
        content = response.content.strip() if response.content else ""
        if not content:
            return []

        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            parsed = json.loads(json_match.group(0))
        else:
            parsed = json.loads(content)

        raw_memories = parsed.get("memories", [])
        if not isinstance(raw_memories, list):
            return []

        valid = []
        for item in raw_memories:
            if not isinstance(item, dict):
                continue
            mem_content = (item.get("content") or "").strip()
            mem_type = item.get("memory_type", "semantic")
            if not mem_content:
                continue
            if mem_type not in ("semantic", "commitment"):
                # Episodische Erinnerungen werden nicht mehr extrahiert —
                # sie kommen durch Tages-Konsolidierung aus der Chat-History
                if mem_type == "episodic":
                    continue
                mem_type = "semantic"
            importance = item.get("importance", 3)
            if not isinstance(importance, int) or importance < 1:
                importance = 3
            importance = min(5, importance)
            tags = item.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip() for t in tags if t]

            entry = {
                "memory_type": mem_type,
                "content": mem_content,
                "importance": importance,
                "tags": tags,
            }
            # related_character: Adressat des Memories. LLM-Output bevorzugt,
            # ansonsten Default = Konversationspartner. Damit Commitments
            # nicht "dem Spieler/User" sondern dem echten Charakter zugeordnet
            # werden.
            related = (item.get("related_character") or "").strip()
            if not related:
                related = partner_name
            # Generische Labels herausfiltern — niemals als Adressat speichern.
            if related.lower() in {"user", "player", "spieler", "the user",
                                    "assistant", "character"}:
                related = partner_name
            if related:
                entry["related_character"] = related
            # Delay fuer Commitments erfassen
            delay = (item.get("delay") or "").strip()
            if delay and mem_type == "commitment":
                entry["delay"] = delay
            valid.append(entry)

        # Erledigte Commitments markieren
        completed_ids = parsed.get("completed_ids", [])
        if isinstance(completed_ids, list) and completed_ids:
            _mark_commitments_completed(character_name, completed_ids)

        return valid

    except Exception as e:
        logger.error("Memory extraction error: %s", e)
        return []


def _mark_commitments_completed(character_name: str, commitment_ids: List[str]
):
    """Markiert Commitments als erledigt (tag 'completed' hinzufuegen)."""
    from app.models.memory import load_memories, save_memories

    entries = load_memories(character_name)
    changed = False
    for entry in entries:
        if entry.get("id") in commitment_ids and entry.get("memory_type") == "commitment":
            tags = entry.get("tags", [])
            if "completed" not in tags:
                tags.append("completed")
                entry["tags"] = tags
                changed = True
                logger.info("Commitment erledigt: %s — %s",
                            entry["id"], entry.get("content", "")[:60])
    if changed:
        save_memories(character_name, entries)


# Adressat-Heuristik fuer Background-Commitments: nur wenn das Plan-Memory
# einen externen Empfaenger benennt, gilt es als echtes commitment. Innere
# Plaene ohne Adressat werden zu semantic herabgestuft.
_ADDRESSEE_RE = re.compile(
    r"\b(verspricht|beauftragt|vereinbart|sagt\s+zu|erinnert|fragt|"
    r"promises|tells|asks|requests|agrees\s+with|reminds)\b",
    re.IGNORECASE,
)


def apply_extracted_memories(character_name: str,
    extracted: List[Dict[str, Any]],
    extraction_context: Optional[Dict[str, Any]] = None) -> int:
    """Speichert extrahierte Memories. Commitments mit Delay werden als Intent eingeplant.

    extraction_context (optional):
      - source: "user_chat" | "thought" | "random_event" | "group_chat" — wo
        die Extraktion ausgeloest wurde
      - is_background: bool — True bei Background-Pfaden (Thought etc.). Bei
        True wird ein commitment ohne delay UND ohne externen Adressaten zu
        semantic umklassifiziert, damit der commitment-Schutz nicht greift.
      - event_id: str — wenn aus einem Random-Event-Kontext, fuer spaeteren
        Cleanup beim Event-Abbruch.
    """
    from app.models.memory import add_memory, load_memories, _keyword_overlap
    from datetime import datetime as _dt, timedelta as _td

    ctx = extraction_context or {}
    is_background = bool(ctx.get("is_background"))
    event_id = ctx.get("event_id") or ""
    source = ctx.get("source") or ""

    # Recent-Memory-Pool fuer Dedup: alle <14d, Inhalt vorbereiten
    recent_cutoff = _dt.now() - _td(days=14)
    recent_contents: List[str] = []
    for e in load_memories(character_name):
        try:
            ts = _dt.fromisoformat(e.get("timestamp", ""))
        except (ValueError, TypeError):
            continue
        if ts >= recent_cutoff:
            recent_contents.append(e.get("content", ""))

    count = 0
    for item in extracted:
        tags = item.get("tags", [])
        mem_type = item.get("memory_type", "semantic")
        delay = item.get("delay", "")
        new_content = item.get("content", "")

        # Dedup: gegen alle <14d alten Memories. Bei >50% Keyword-Overlap skip,
        # damit nicht jede Variation desselben Plans ("Wanzen installieren" /
        # "Wanzen in der Lagerhalle installieren") einen eigenen Eintrag bekommt.
        if new_content and any(_keyword_overlap(c, new_content) > 0.5 for c in recent_contents):
            continue

        # Background-Pfad: commitment ohne delay UND ohne externen Adressaten
        # → semantic. Bleibt als Fakt erhalten, faellt aber unter den 50er-Cap
        # statt unter den commitment-Schutz.
        if is_background and mem_type == "commitment" and not delay:
            if not _ADDRESSEE_RE.search(new_content or ""):
                mem_type = "semantic"

        # Commitment mit Zeitangabe → Intent erzeugen
        intent_created = False
        if mem_type == "commitment" and delay:
            _create_intent_from_commitment(character_name, item["content"], delay
            )
            tags = list(tags) + ["intent_created"]
            intent_created = True

        importance = item.get("importance", 3)
        # Auto-extrahierte Plaene aus Background-Generation (Activities/Thoughts/
        # Random Events) werden vom Extraction-LLM oft mit imp 4-5 bewertet, weil
        # die Story-Inhalte dramatisch klingen. Das fuehrt zu Backlog-Inflation
        # und schuetzt sie vor dem Auto-Cleanup. Echte Wichtigkeit wird durch den
        # User-Kontext bestimmt, nicht durch das LLM.
        if intent_created and importance > 3:
            importance = 3

        # Provenance ins Meta — fuer spaeteren Event-Cleanup und Debugging
        extra_meta: Dict[str, Any] = {}
        if source:
            extra_meta["source"] = source
        if event_id:
            extra_meta["event_id"] = event_id

        result = add_memory(
            character_name=character_name,
            content=item["content"],
            memory_type=mem_type,
            importance=importance,
            tags=tags,
            related_character=item.get("related_character", ""),
            extra_meta=extra_meta or None)
        if result:
            count += 1
            recent_contents.append(new_content)
    return count


def _create_intent_from_commitment(character_name: str, content: str, delay: str
):
    """Erzeugt einen remind-Intent aus einem Commitment mit Zeitangabe."""
    try:
        from app.core.intent_engine import Intent, execute_intent

        # Delay normalisieren
        delay_seconds = _parse_commitment_delay(delay)
        if delay_seconds <= 0:
            return

        intent = Intent(
            type="remind",
            delay_seconds=delay_seconds,
            params={"note": content, "message": content},
            raw=f"[auto-commitment] {content}")

        # Scheduler holen fuer deferred Intents
        scheduler = None
        try:
            from app.core.thoughts import get_thought_runner
            pl = get_thought_runner()
            if pl:
                scheduler = getattr(pl, '_scheduler', None)
        except Exception:
            pass

        execute_intent(intent, character_name, scheduler_manager=scheduler)
        logger.info("Commitment → Intent: '%s' in %ds fuer %s",
                     content[:60], delay_seconds, character_name)

    except Exception as e:
        logger.warning("Commitment→Intent Fehler: %s", e)


def _parse_commitment_delay(delay: str) -> int:
    """Parst natuerlichsprachige Zeitangaben zu Sekunden.

    Unterstuetzt: 30m, 2h, 1d, morgen, spaeter, uebermorgen, HH:MM
    """
    delay = delay.strip().lower()
    if not delay:
        return 0

    # Relative: 30m, 2h, 1d
    m = re.match(r'^(\d+)\s*(m|min|h|hr|d|tag)e?$', delay)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit in ('m', 'min'):
            return val * 60
        elif unit in ('h', 'hr'):
            return val * 3600
        elif unit in ('d', 'tag'):
            return val * 86400

    # Natuerlichsprachig
    if 'morgen' in delay and 'uebermorgen' not in delay:
        return 16 * 3600  # ~morgen frueh (16h von jetzt)
    if 'uebermorgen' in delay:
        return 40 * 3600
    if 'spaeter' in delay or 'later' in delay:
        return 2 * 3600  # 2 Stunden
    if 'bald' in delay or 'soon' in delay:
        return 1 * 3600
    if 'naechste woche' in delay or 'next week' in delay:
        return 7 * 86400
    if 'heute abend' in delay or 'tonight' in delay:
        return 6 * 3600

    # HH:MM Format
    time_match = re.match(r'^(\d{1,2}):(\d{2})$', delay)
    if time_match:
        target_h = int(time_match.group(1))
        target_m = int(time_match.group(2))
        now = datetime.now()
        target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # Morgen zur gleichen Zeit
        diff = (target - now).total_seconds()
        return max(60, int(diff))

    return 0


# ---------------------------------------------------------------------------
# Consolidation (Background-Worker)
# ---------------------------------------------------------------------------

def consolidate_memories(character_name: str) -> int:
    """Konsolidiert alte Memories: Decay anwenden, aehnliche zusammenfassen.

    Wird periodisch im Hintergrund aufgerufen.
    Returns Anzahl entfernter/archivierter Entries.
    """
    import os as _os
    from app.models.memory import load_memories, save_memories, _compute_decay

    entries = load_memories(character_name)
    if len(entries) < 30:
        return 0  # Zu wenige zum Konsolidieren

    removed = 0
    now = datetime.now()

    # Konfiguration
    commitment_max_days = int(_os.environ.get("MEMORY_COMMITMENT_MAX_DAYS", "7"))
    completed_max_days = int(_os.environ.get("MEMORY_COMMITMENT_COMPLETED_DAYS", "3"))
    semantic_max = int(_os.environ.get("MEMORY_MAX_SEMANTIC", "50"))

    # 1. Commitments cleanup:
    #    a) "completed" → nach 3 Tagen weg
    #    b) ALTE Commitments ohne completion → nach commitment_max_days weg
    #       (auto-generierte intent_created Plans sammeln sich sonst zu hunderten)
    pre_commitment = []
    for entry in entries:
        if entry.get("memory_type") == "commitment":
            tags = entry.get("tags", []) or []
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                age_days = (now - ts).total_seconds() / 86400
                if "completed" in tags and age_days > completed_max_days:
                    removed += 1
                    logger.debug("Completed commitment entfernt (%.0f Tage): %s",
                                 age_days, entry.get("content", "")[:60])
                    continue
                # Important-Tag schuetzt vor Auto-Cleanup
                important = "important" in tags or entry.get("importance", 3) >= 4
                if not important and age_days > commitment_max_days:
                    removed += 1
                    logger.debug("Stale commitment entfernt (%.0f Tage, ohne completion): %s",
                                 age_days, entry.get("content", "")[:60])
                    continue
            except (ValueError, TypeError):
                pass
        pre_commitment.append(entry)

    # 2. Decay berechnen und sehr schwache archivieren
    active = []
    for entry in pre_commitment:
        decay = _compute_decay(entry)
        entry["decay_factor"] = round(decay, 3)
        if decay < 0.1 and entry.get("importance", 3) <= 2:
            removed += 1
            continue  # Archivieren (entfernen)
        active.append(entry)

    # 3. Duplikat-Erkennung (exakt gleicher Content)
    seen_content = set()
    deduped = []
    for entry in active:
        content_key = entry.get("content", "").strip().lower()
        if content_key in seen_content:
            removed += 1
            continue
        seen_content.add(content_key)
        deduped.append(entry)

    # 4. Semantic-Cap enforcement: wenn Backlog ueber Cap → schwaechste raus.
    #    (Reaktiver Cap-Check beim add greift nur auf neue Adds, alte Backlogs
    #    blieben unangetastet. Hier gleichen wir das einmal pro Konsolidierung
    #    nach.)
    semantic = [e for e in deduped if e.get("memory_type") == "semantic"
                and "relationship" not in (e.get("tags") or [])]
    if len(semantic) > semantic_max:
        # Score: importance * decay * (1 + access_bonus). Niedrigster Score zuerst.
        def _score(e):
            imp = e.get("importance", 3)
            decay = e.get("decay_factor", 1.0)
            access = min(0.3, e.get("access_count", 0) * 0.05)
            return imp * decay * (1.0 + access)
        sorted_sem = sorted(semantic, key=_score)
        excess = len(semantic) - semantic_max
        kill_ids = {e.get("id") for e in sorted_sem[:excess]}
        deduped = [e for e in deduped if e.get("id") not in kill_ids]
        removed += len(kill_ids)
        logger.info("Semantic-Cap [%s]: %d Eintraege ueber Cap (%d) entfernt",
                    character_name, len(kill_ids), semantic_max)

    if removed > 0:
        save_memories(character_name, deduped)
        logger.info("Konsolidiert %s: %d Memories entfernt", character_name, removed)

    return removed


def handle_memory_consolidation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task-Queue Handler: 3-Stufen-Konsolidierung fuer einen Character.

    Pipeline:
      Phase 1: Cleanup (Duplikate, erledigte Commitments, Decay)
      Phase 2: Tages-Konsolidierung (Episodics → Tages-Summary, Originale loeschen)
      Phase 3: Wochen-Konsolidierung (Tages-Summaries → Wochen-Summary)
      Phase 4: Monats-Konsolidierung (Wochen-Summaries → Monats-Summary)
      Phase 5: Tages-Summaries aktualisieren + Backfill
    """
    character_name = payload.get("character_name", "")
    if not character_name:
        return {"error": "character_name missing"}

    total = 0

    # Phase 1: Cleanup
    try:
        removed = consolidate_memories(character_name)
        total += removed
    except Exception as e:
        logger.error("Consolidation Phase 1 error %s: %s", character_name, e)

    # Phase 2: Tages-Konsolidierung (Episodics → Tages-Summary)
    try:
        removed_daily = _consolidate_episodics_to_daily(character_name)
        total += removed_daily
    except Exception as e:
        logger.error("Consolidation Phase 2 error %s: %s", character_name, e)

    # Phase 3: Wochen-Konsolidierung
    try:
        removed_weekly = _consolidate_daily_to_weekly(character_name)
        total += removed_weekly
    except Exception as e:
        logger.error("Consolidation Phase 3 error %s: %s", character_name, e)

    # Phase 4: Monats-Konsolidierung
    try:
        removed_monthly = _consolidate_weekly_to_monthly(character_name)
        total += removed_monthly
    except Exception as e:
        logger.error("Consolidation Phase 4 error %s: %s", character_name, e)

    # Phase 5: Tages-Summaries aktualisieren + Backfill (via Router/Task=consolidation)
    try:
        from app.utils.history_manager import _update_daily_summary, backfill_missing_daily_summaries
        _update_daily_summary(character_name)
        backfill_missing_daily_summaries(character_name)
    except Exception as e:
        logger.error("Consolidation Phase 5 (daily summaries) error %s: %s", character_name, e)

    return {"success": True, "character": character_name, "removed": total}


def submit_consolidation_for_all():
    """Erstellt Consolidation-Tasks fuer alle Characters in der Queue."""
    from app.models.character import list_available_characters
    from app.core.background_queue import get_background_queue

    bq = get_background_queue()
    count = 0
    for char_name in list_available_characters():
        bq.submit(
            task_type="memory_consolidation",
            payload={"character_name": char_name},
            priority=30,
            agent_name=char_name,
            deduplicate=True)
        count += 1

    if count:
        logger.info("Memory-Konsolidierung: %d Tasks eingereicht", count)


def register_consolidation_handler():
    """Registriert den Consolidation-Handler in der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("memory_consolidation", handle_memory_consolidation)
    logger.info("Memory Consolidation Handler registriert")


# Legacy-Wrapper (fuer bestehende Aufrufe)
def run_consolidation_for_all_users():
    """Erstellt Consolidation-Tasks in der Queue (non-blocking)."""
    submit_consolidation_for_all()


def _llm_summarize(system_prompt: str, user_prompt: str, character_name: str) -> str:
    """Ruft LLM fuer eine Zusammenfassung auf. Returns leeren String bei Fehler."""
    try:
        from app.core.llm_router import llm_call
        response = llm_call(
            task="consolidation",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            agent_name=character_name)
        result = (response.content or "").strip() if response else ""
        # LLM-Artefakte bereinigen
        result = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', result).strip()
        return result if len(result) >= 20 else ""
    except Exception as e:
        logger.error("LLM-Summarize Fehler fuer %s: %s", character_name, e)
        return ""


# ---------------------------------------------------------------------------
# Phase 2: Tages-Konsolidierung (Episodics → Tages-Summary)
# ---------------------------------------------------------------------------

def _consolidate_episodics_to_daily(character_name: str) -> int:
    """Konsolidiert episodische Memories aelter als SHORT_TERM_DAYS zu Tages-Summaries.

    Pro Tag: Episodische Memories + bestehende Tages-Summary → neue Tages-Summary.
    Episodische Originale werden geloescht.
    """
    from app.models.memory import load_memories, save_memories
    from app.utils.history_manager import (get_memory_thresholds,
                                            load_daily_summaries_combined,
                                            save_daily_summary)

    thresholds = get_memory_thresholds()
    cutoff = datetime.now() - timedelta(days=thresholds["short_term_days"])

    entries = load_memories(character_name)

    # Episodische Memories aelter als Kurzzeit nach Tag gruppieren
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if e.get("memory_type") != "episodic":
            continue
        try:
            ts = datetime.fromisoformat(e.get("timestamp", ""))
            if ts >= cutoff:
                continue  # Zu frisch
            day_str = ts.strftime("%Y-%m-%d")
            by_day.setdefault(day_str, []).append(e)
        except (ValueError, TypeError):
            continue

    if not by_day:
        return 0

    # Episodische → Tages-Konsolidierung schreibt in den partner-leeren Slot
    # ('') der summaries-Tabelle. Vorhandene Texte (alle Partner kombiniert)
    # werden als Kontext gelesen, damit Episodics nicht widersprechen.
    existing_daily = load_daily_summaries_combined(character_name)
    removed_total = 0
    ids_to_remove = set()

    # Max 3 Tage pro Durchlauf konsolidieren (LLM-Budget)
    days_processed = 0
    for day_str, day_entries in sorted(by_day.items()):
        if days_processed >= 3:
            break

        contents = "\n".join(f"- {e.get('content', '')}" for e in day_entries if e.get('content', '').strip())
        if not contents:
            # Alle Episodics dieses Tages sind leer → nur loeschen
            for e in day_entries:
                ids_to_remove.add(e.get("id"))
            removed_total += len(day_entries)
            continue
        existing = existing_daily.get(day_str, "")

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "consolidation_daily",
            day_str=day_str,
            character_name=character_name,
            existing=existing,
            contents=contents)

        summary = _llm_summarize(sys_prompt, user_prompt, character_name)
        if summary:
            save_daily_summary(character_name, day_str, summary)
            for e in day_entries:
                ids_to_remove.add(e.get("id"))
            removed_total += len(day_entries)
            days_processed += 1
            logger.info("Tages-Konsolidierung %s [%s]: %d Episodics → Summary",
                        character_name, day_str, len(day_entries))

    # Episodische Originale loeschen
    if ids_to_remove:
        new_entries = [e for e in entries if e.get("id") not in ids_to_remove]
        save_memories(character_name, new_entries)

    return removed_total


# ---------------------------------------------------------------------------
# Phase 3: Wochen-Konsolidierung (Tages-Summaries → Wochen-Summary)
# ---------------------------------------------------------------------------

def _get_weekly_summaries_path(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "weekly_summaries.json"


def load_weekly_summaries(character_name: str) -> Dict[str, str]:
    """Laedt Wochen-Summaries. Returns {week_key: summary_text}. Key = 'YYYY-WNN'."""
    path = _get_weekly_summaries_path(character_name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("summaries", {})
        except Exception:
            pass
    return {}


def save_weekly_summary(character_name: str, week_key: str, summary: str):
    path = _get_weekly_summaries_path(character_name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    summaries = data.get("summaries", {})
    summaries[week_key] = summary
    data["summaries"] = summaries
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _consolidate_daily_to_weekly(character_name: str) -> int:
    """Konsolidiert Tages-Summaries aelter als MID_TERM_DAYS zu Wochen-Summaries."""
    from app.utils.history_manager import (get_memory_thresholds,
                                            load_daily_summaries_combined)

    thresholds = get_memory_thresholds()
    cutoff = (datetime.now() - timedelta(days=thresholds["mid_term_days"])).date()

    # Wochen-Konsolidierung verdichtet ueber alle Partner — kombinierter
    # Text pro Tag (alle Partner-Slots zusammen).
    daily = load_daily_summaries_combined(character_name)
    if not daily:
        return 0

    # Tages-Summaries nach Kalenderwoche gruppieren (leere ueberspringen)
    from datetime import date as date_type
    by_week: Dict[str, Dict[str, str]] = {}  # {week_key: {date: summary}}
    empty_days = []  # Leere Eintraege zum Aufraeumen
    for day_str, summary in daily.items():
        try:
            d = date_type.fromisoformat(day_str)
            if d >= cutoff:
                continue  # Zu frisch
            if not summary or not summary.strip():
                empty_days.append(day_str)  # Leere Eintraege merken
                continue
            iso = d.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
            by_week.setdefault(week_key, {})[day_str] = summary
        except (ValueError, TypeError):
            continue

    if not by_week and not empty_days:
        return 0

    existing_weekly = load_weekly_summaries(character_name)
    removed_total = 0
    days_to_remove = list(empty_days)  # Leere Eintraege immer loeschen

    # Max 2 Wochen pro Durchlauf
    weeks_processed = 0
    for week_key, week_days in sorted(by_week.items()):
        if weeks_processed >= 2:
            break
        if week_key in existing_weekly:
            # Bereits konsolidiert — Tages-Summaries loeschen
            days_to_remove.extend(week_days.keys())
            removed_total += len(week_days)
            continue

        entries_text = "\n".join(
            f"- {d}: {s}" for d, s in sorted(week_days.items()) if s and s.strip()
        )
        if not entries_text:
            # Alle Eintraege dieser Woche sind leer → nur loeschen
            days_to_remove.extend(week_days.keys())
            removed_total += len(week_days)
            continue

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "consolidation_weekly",
            week_key=week_key,
            character_name=character_name,
            entries_text=entries_text)

        summary = _llm_summarize(sys_prompt, user_prompt, character_name)
        if summary:
            save_weekly_summary(character_name, week_key, summary)
            days_to_remove.extend(week_days.keys())
            removed_total += len(week_days)
            weeks_processed += 1
            logger.info("Wochen-Konsolidierung %s [%s]: %d Tage → Summary",
                        character_name, week_key, len(week_days))

    if days_to_remove:
        from app.utils.history_manager import delete_daily_summaries
        delete_daily_summaries(character_name, days_to_remove)

    return removed_total


# ---------------------------------------------------------------------------
# Phase 4: Monats-Konsolidierung (Wochen-Summaries → Monats-Summary)
# ---------------------------------------------------------------------------

def _get_monthly_summaries_path(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "monthly_summaries.json"


def load_monthly_summaries(character_name: str) -> Dict[str, str]:
    """Laedt Monats-Summaries. Returns {month_key: summary_text}. Key = 'YYYY-MM'."""
    path = _get_monthly_summaries_path(character_name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("summaries", {})
        except Exception:
            pass
    return {}


def save_monthly_summary(character_name: str, month_key: str, summary: str):
    path = _get_monthly_summaries_path(character_name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    summaries = data.get("summaries", {})
    summaries[month_key] = summary
    data["summaries"] = summaries
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _consolidate_weekly_to_monthly(character_name: str) -> int:
    """Konsolidiert Wochen-Summaries aelter als LONG_TERM_DAYS zu Monats-Summaries."""
    from app.utils.history_manager import get_memory_thresholds

    thresholds = get_memory_thresholds()
    cutoff = (datetime.now() - timedelta(days=thresholds["long_term_days"])).date()

    weekly = load_weekly_summaries(character_name)
    if not weekly:
        return 0

    # Wochen nach Monat gruppieren
    from datetime import date as date_type
    by_month: Dict[str, Dict[str, str]] = {}  # {month_key: {week_key: summary}}
    for week_key, summary in weekly.items():
        try:
            # week_key = "YYYY-WNN" → Montag der Woche
            year, wk = week_key.split("-W")
            d = date_type.fromisocalendar(int(year), int(wk), 1)
            if d >= cutoff:
                continue
            month_key = d.strftime("%Y-%m")
            by_month.setdefault(month_key, {})[week_key] = summary
        except (ValueError, TypeError):
            continue

    if not by_month:
        return 0

    existing_monthly = load_monthly_summaries(character_name)
    removed_total = 0
    weeks_to_remove = []

    # Max 1 Monat pro Durchlauf
    for month_key, month_weeks in sorted(by_month.items()):
        if month_key in existing_monthly:
            weeks_to_remove.extend(month_weeks.keys())
            removed_total += len(month_weeks)
            continue

        entries_text = "\n".join(
            f"- {w}: {s}" for w, s in sorted(month_weeks.items())
        )

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "consolidation_monthly",
            month_key=month_key,
            character_name=character_name,
            entries_text=entries_text)

        summary = _llm_summarize(sys_prompt, user_prompt, character_name)
        if summary:
            save_monthly_summary(character_name, month_key, summary)
            weeks_to_remove.extend(month_weeks.keys())
            removed_total += len(month_weeks)
            logger.info("Monats-Konsolidierung %s [%s]: %d Wochen → Summary",
                        character_name, month_key, len(month_weeks))
        break  # Max 1 Monat pro Durchlauf

    # Wochen-Eintraege loeschen
    if weeks_to_remove:
        remaining = {w: s for w, s in weekly.items() if w not in weeks_to_remove}
        path = _get_weekly_summaries_path(character_name)
        path.write_text(json.dumps({"summaries": remaining}, ensure_ascii=False, indent=2), encoding="utf-8")

    return removed_total


def run_migration_for_all_users():
    """Migriert knowledge.json -> memories.json fuer alle User/Characters.

    Fuehrt auch die 3-Stufen-Migration durch (Episodics → Tages/Wochen/Monats-Summaries).
    """
    from app.models.memory import migrate_knowledge_to_memories
    from app.models.character import list_available_characters

    total = 0
    for char_name in list_available_characters():
        try:
            migrated = migrate_knowledge_to_memories(char_name)
            total += migrated
        except Exception as e:
            logger.error("Migration error %s: %s", char_name, e)

    if total > 0:
        logger.info("Knowledge-Migration abgeschlossen: %d Eintraege migriert", total)

    submit_three_tier_migration()


def submit_three_tier_migration():
    """Reiht 3-Tier-Migrations-Jobs in die Background-Queue ein."""
    from app.models.character import list_available_characters, get_character_dir
    from app.core.background_queue import get_background_queue

    bq = get_background_queue()
    count = 0
    for char_name in list_available_characters():
        marker = get_character_dir(char_name) / ".migrated_3tier"
        if marker.exists():
            continue
        bq.submit(
            task_type="three_tier_migration",
            payload={"character_name": char_name},
            priority=30,
            agent_name=char_name,
            deduplicate=False,
        )
        count += 1

    if count:
        logger.info("3-Tier Migration: %d Jobs eingereicht", count)


def handle_three_tier_migration(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler: Migriert einen Character ins 3-Stufen-Format."""
    user_id = payload.get("user_id", "")
    character_name = payload.get("character_name", "")
    if not user_id or not character_name:
        return {"error": "user_id or character_name missing"}

    from app.models.character import get_character_dir
    marker = get_character_dir(character_name) / ".migrated_3tier"
    if marker.exists():
        return {"skipped": True, "reason": "already migrated"}

    try:
        migrated = _migrate_three_tier(character_name)
        if migrated >= 0:
            marker.write_text(datetime.now().isoformat(), encoding="utf-8")
            logger.info("3-Tier Migration %s/%s: %d Episodics konsolidiert", character_name, migrated)
            return {"success": True, "character": character_name, "migrated": migrated}
        return {"error": "migration returned -1"}
    except Exception as e:
        logger.error("3-Tier Migration error %s/%s: %s", character_name, e)
        return {"error": str(e)}


def register_migration_handler():
    """Registriert den Migration-Handler in der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("three_tier_migration", handle_three_tier_migration)
    logger.info("3-Tier Migration Handler registriert")


def _migrate_three_tier(character_name: str) -> int:
    """Migriert einen Character ins 3-Stufen-Format.

    1. Episodische Memories aelter als SHORT_TERM_DAYS → Tages-Summaries
    2. Tages-Summaries aelter als MID_TERM_DAYS → Wochen-Summaries
    3. Wochen-Summaries aelter als LONG_TERM_DAYS → Monats-Summaries

    Returns Anzahl konsolidierter Episodics, oder -1 bei Fehler.
    """
    from app.models.memory import load_memories, save_memories
    from app.utils.history_manager import (get_memory_thresholds,
                                            load_daily_summaries_combined,
                                            save_daily_summary)

    thresholds = get_memory_thresholds()
    cutoff = datetime.now() - timedelta(days=thresholds["short_term_days"])

    entries = load_memories(character_name)
    if not entries:
        return 0

    # Phase 1: Episodische Memories → Tages-Summaries
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if e.get("memory_type") != "episodic":
            continue
        try:
            ts = datetime.fromisoformat(e.get("timestamp", ""))
            if ts >= cutoff:
                continue
            day_str = ts.strftime("%Y-%m-%d")
            by_day.setdefault(day_str, []).append(e)
        except (ValueError, TypeError):
            continue

    if not by_day:
        # Keine alten Episodics — trotzdem Wochen/Monats-Konsolidierung versuchen
        _consolidate_daily_to_weekly(character_name)
        _consolidate_weekly_to_monthly(character_name)
        return 0

    existing_daily = load_daily_summaries_combined(character_name)
    ids_to_remove = set()
    total_migrated = 0

    for day_str, day_entries in sorted(by_day.items()):
        # Wenn schon eine Tages-Summary existiert, Episodics direkt loeschen
        if day_str in existing_daily and existing_daily[day_str]:
            for e in day_entries:
                ids_to_remove.add(e.get("id"))
            total_migrated += len(day_entries)
            continue

        # LLM-Summary generieren
        contents = "\n".join(f"- {e.get('content', '')}" for e in day_entries)
        prompt = f"""Fasse den Tag {day_str} fuer {character_name} zusammen.

Einzelne Erinnerungen dieses Tages:
{contents}

Schreibe 3-5 kompakte Saetze aus der Perspektive von {character_name} (dritte Person).
Fokussiere auf: Schluesselmomente, beteiligte Personen, Emotionen, Entscheidungen.
Antworte NUR mit der Zusammenfassung."""

        summary = _llm_summarize(prompt, character_name)
        if summary:
            save_daily_summary(character_name, day_str, summary)
            for e in day_entries:
                ids_to_remove.add(e.get("id"))
            total_migrated += len(day_entries)
        # Wenn LLM fehlschlaegt: Episodics bleiben, naechster Versuch beim naechsten Start

    # Episodische Originale loeschen
    if ids_to_remove:
        new_entries = [e for e in entries if e.get("id") not in ids_to_remove]
        save_memories(character_name, new_entries)

    # Phase 2+3: Wochen/Monats-Konsolidierung
    _consolidate_daily_to_weekly(character_name)
    _consolidate_weekly_to_monthly(character_name)

    return total_migrated

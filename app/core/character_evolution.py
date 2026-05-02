"""Character Evolution — periodischer Trigger fuer Selbst-Reflexion.

Statt selbst LLM-Calls zu machen und ins Profil zu schreiben (alt), triggert
diese Datei jetzt einen forcierten Gedanken (forced_thought) pro Character mit
einem Context-Hint zur Reflexion. Der Thought-LLM nutzt das `EditSelf`-Tool
um Beliefs/Lessons/Goals selbst anzupassen — heading-basiert in den MD-Files.

Ablauf pro Tick:
  1. Pro Character pruefen ob CHARACTER_EVOLUTION_INTERVAL_HOURS seit letztem
     Update vergangen sind (character_evolution_updated im Profil).
  2. Memories der letzten N Tage auswaehlen + Relationship-Context.
  3. Context-Hint mit Reflexionsauftrag bauen.
  4. forced_thought-Task in BackgroundQueue einstellen.

Konfiguration via ENV:
    CHARACTER_EVOLUTION_ENABLED=true              (default: true)
    CHARACTER_EVOLUTION_INTERVAL_HOURS=24         (default: 24)
    CHARACTER_EVOLUTION_MIN_MEMORIES=5            (default: 5)
"""
import os
from datetime import datetime
from typing import Any, Dict, List

from app.core.log import get_logger

logger = get_logger("character_evolution")

ENABLED = os.environ.get("CHARACTER_EVOLUTION_ENABLED", "true").lower() in ("true", "1", "yes")
INTERVAL_HOURS = int(os.environ.get("CHARACTER_EVOLUTION_INTERVAL_HOURS", "24"))
MIN_MEMORIES = int(os.environ.get("CHARACTER_EVOLUTION_MIN_MEMORIES", "5"))


def handle_character_evolution(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler: triggert forcierte Reflexions-Gedanken."""
    if not ENABLED:
        return {"skipped": True, "reason": "disabled"}

    user_id = payload.get("user_id", "")
    if not user_id:
        return {"error": "user_id missing"}

    task_id = payload.get("_task_id", "")

    from app.models.character import list_available_characters, is_character_sleeping
    from app.models.character_template import is_feature_enabled

    characters = list_available_characters()
    if not characters:
        return {"skipped": True, "reason": "no characters"}

    triggered = 0

    for char_name in characters:
        if is_character_sleeping(char_name):
            continue
        if task_id and _is_cancelled(task_id):
            logger.info("Task %s abgebrochen, stoppe", task_id)
            return {"aborted": True, "triggered": triggered}

        # Reflexion macht nur Sinn wenn der Char editierbare Soul-Felder hat
        try:
            has_editable = (
                is_feature_enabled(char_name, "beliefs_enabled")
                or is_feature_enabled(char_name, "lessons_enabled")
                or is_feature_enabled(char_name, "goals_enabled")
            )
        except Exception:
            has_editable = True
        if not has_editable:
            continue

        try:
            if _trigger_reflection(char_name):
                triggered += 1
        except Exception as e:
            logger.error("Reflexions-Trigger Fehler bei %s: %s", char_name, e)

    if triggered:
        logger.info("Character Evolution: %d Reflexionen getriggert (user=%s)",
                    triggered)
    return {"success": True, "triggered": triggered}


def _is_cancelled(task_id: str) -> bool:
    try:
        from app.core.task_queue import get_task_queue
        return get_task_queue().is_task_cancelled(task_id)
    except Exception:
        return False


def _trigger_reflection(character_name: str) -> bool:
    """Triggert einen forcierten Gedanken zur Selbst-Reflexion fuer einen Character.

    Returns True wenn ein Trigger eingestellt wurde.
    """
    from app.models.character import get_character_profile
    from app.models.memory import load_memories

    profile = get_character_profile(character_name)

    # 24h-Cooldown via Profil-Marker
    last_updated = profile.get("character_evolution_updated", "")
    if last_updated:
        try:
            last_dt = datetime.fromisoformat(last_updated)
            age_hours = (datetime.now() - last_dt).total_seconds() / 3600
            if age_hours < INTERVAL_HOURS:
                return False
        except (ValueError, TypeError):
            pass

    # Memories pruefen
    memories = load_memories(character_name)
    if len(memories) < MIN_MEMORIES:
        logger.debug("%s: nur %d Memories — Reflexion uebersprungen",
                     character_name, len(memories))
        return False

    relevant = _select_relevant_memories(memories)
    if not relevant:
        return False

    # Context-Hint bauen — kompakt, Reflexionsauftrag + Memory-Highlights
    rel_ctx = _get_relationship_context(character_name)
    memory_lines = []
    for m in relevant[:15]:  # max 15 Memories im Hint
        ts = (m.get("timestamp", "") or "")[:10]
        content = (m.get("content", "") or "").strip()
        if not content:
            continue
        memory_lines.append(f"- [{ts}] {content[:200]}")
    memory_block = "\n".join(memory_lines) if memory_lines else "(keine relevanten)"

    rel_block = ""
    if rel_ctx:
        rel_block = f"\n\nDeine wichtigen Beziehungen:\n{rel_ctx[:600]}"

    context_hint = (
        f"# Selbst-Reflexion\n"
        f"Du hast in den letzten Tagen einiges erlebt. Reflektiere kurz:\n"
        f"- Haben sich deine Ueberzeugungen veraendert? (beliefs.md)\n"
        f"- Hast du etwas Wichtiges gelernt? (lessons.md)\n"
        f"- Sind deine Ziele noch dieselben? (goals.md)\n\n"
        f"Wichtig: Veraendere nur was sich durch ECHTE Erfahrungen geaendert hat. "
        f"Nutze das Tool **EditSelf** mit operation='append' fuer Neues, "
        f"oder 'replace_section' wenn etwas grundlegend anders ist. "
        f"Wenn nichts substantiell neu ist: einfach SKIP.\n\n"
        f"Erinnerungen aus den letzten Tagen:\n{memory_block}"
        f"{rel_block}"
    )

    # NOTE: character_evolution is being phased out in favour of the new
    # RetrospectSkill (which writes beliefs.md / improvements.md). This
    # legacy path used to submit a forced_thought with a self-reflection
    # context_hint; with forced_thoughts removed (B1), the path is a
    # no-op now. Bump the agent so they may decide to call Retrospect on
    # their next turn (the retrospective_block in agent_thought.md hints
    # "time to reflect" when overdue).
    try:
        from app.core.agent_loop import get_agent_loop
        get_agent_loop().bump(character_name)
        from app.models.character import save_character_profile
        profile["character_evolution_updated"] = datetime.now().isoformat()
        save_character_profile(character_name, profile)
        logger.info("Evolution -> AgentLoop bump: %s (%d relevant memories)",
                    character_name, len(relevant))
        return True
    except Exception as e:
        logger.error("Evolution bump failed for %s: %s", character_name, e)
        return False


def _select_relevant_memories(
    memories: List[Dict[str, Any]],
    max_count: int = 30) -> List[Dict[str, Any]]:
    """Waehlt die relevantesten Memories fuer die Reflexion (Recency + Importance)."""
    candidates = []
    now = datetime.now()

    for entry in memories:
        mtype = entry.get("memory_type", "")
        if mtype not in ("episodic", "semantic"):
            continue
        content = entry.get("content", "").strip()
        if not content:
            continue

        importance = entry.get("importance", 3)

        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            age_days = max(0, (now - ts).total_seconds() / 86400)
        except (ValueError, TypeError):
            age_days = 30.0

        recency = 2.0 if age_days <= 7 else (1.0 if age_days <= 30 else 0.5)
        score = importance * recency
        candidates.append((score, entry))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in candidates[:max_count]]


def _get_relationship_context(character_name: str) -> str:
    """Laedt Relationship-Summaries als kompakten Kontext-String."""
    try:
        from app.models.memory import get_memories_by_tag
        entries = get_memories_by_tag(character_name, tag="relationship")
        summaries = []
        for e in entries[:10]:
            related = e.get("related_character", "")
            summary = e.get("summary", "")
            if related and summary:
                summaries.append(f"- {related}: {summary[:150]}")
        return "\n".join(summaries) if summaries else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_character_evolution_handler():
    """Registriert den Handler in der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("character_evolution", handle_character_evolution)
    logger.info("Character Evolution Handler registriert")

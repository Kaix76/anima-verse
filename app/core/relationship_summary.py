"""Relationship Summary — Periodischer Background-Job der character_relationship
Eintraege per LLM zu narrativen Beziehungs-Zusammenfassungen verdichtet.

Statt roher Event-Logs ("Commented on X's Instagram: ...") erhaelt jede
Beziehung eine kurze narrative Summary die in den System-Prompt injiziert wird.

Laeuft in der BackgroundQueue, getriggert via ThoughtLoop.

Konfiguration via ENV:
    RELATIONSHIP_SUMMARY_ENABLED=true          (default: true)
    RELATIONSHIP_SUMMARY_INTERVAL_MINUTES=30   (default: 30)
"""
import os
import re
from typing import Any, Dict, Optional

from app.core.log import get_logger

logger = get_logger("relationship_summary")

ENABLED = os.environ.get("RELATIONSHIP_SUMMARY_ENABLED", "true").lower() in ("true", "1", "yes")
INTERVAL_MINUTES = int(os.environ.get("RELATIONSHIP_SUMMARY_INTERVAL_MINUTES", "30"))
MAX_SUMMARIES_PER_RUN = int(os.environ.get("RELATIONSHIP_SUMMARY_MAX_PER_RUN", "5"))


def handle_relationship_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler: Generiert Summaries fuer stale character_relationships."""
    if not ENABLED:
        return {"skipped": True, "reason": "disabled"}

    user_id = payload.get("user_id", "")
    if not user_id:
        return {"error": "user_id missing"}

    task_id = payload.get("_task_id", "")

    from app.models.character import list_available_characters, is_character_sleeping

    characters = list_available_characters()
    if not characters:
        return {"skipped": True, "reason": "no characters"}

    total_updates = 0
    remaining_budget = MAX_SUMMARIES_PER_RUN

    from app.models.character import get_character_config
    from app.models.character_template import is_feature_enabled
    for char_name in characters:
        # Sleep-Check: Schlafende Characters ueberspringen
        if is_character_sleeping(char_name):
            continue
        # Feature-Gate: relationship_summary oder relationships aus -> skip
        try:
            if not is_feature_enabled(char_name, "relationship_summary_enabled"):
                continue
            if not is_feature_enabled(char_name, "relationships_enabled"):
                continue
        except Exception:
            pass
        # Legacy per-character relationships_enabled Flag
        try:
            cfg = get_character_config(char_name)
            if str(cfg.get("relationships_enabled", "true")).lower() == "false":
                continue
        except Exception:
            pass
        if remaining_budget <= 0:
            logger.info("Batch-Limit erreicht (%d), Rest im naechsten Run", MAX_SUMMARIES_PER_RUN)
            break
        # Check if task was cancelled externally
        if task_id and _is_cancelled(task_id):
            logger.info("Task %s wurde abgebrochen, stoppe", task_id)
            return {"aborted": True, "updates": total_updates}
        try:
            updates = _summarize_stale_relationships(char_name, max_summaries=remaining_budget)
            total_updates += updates
            remaining_budget -= updates
        except Exception as e:
            logger.error("Fehler bei %s: %s", char_name, e)

    if total_updates > 0:
        logger.info("Relationship Summary: %d Updates fuer User %s", total_updates)

    return {"success": True, "updates": total_updates}


def _is_cancelled(task_id: str) -> bool:
    """Prueft ob der TaskQueue-Task abgebrochen wurde."""
    try:
        from app.core.task_queue import get_task_queue
        return get_task_queue().is_task_cancelled(task_id)
    except Exception:
        return False


def _summarize_stale_relationships(character_name: str, max_summaries: int = 0) -> int:
    """Findet alle stale relationship Eintraege und generiert Summaries."""
    from app.models.memory import load_memories, save_memories

    entries = load_memories(character_name)
    stale = [
        e for e in entries
        if "relationship" in e.get("tags", [])
        and e.get("summary_stale", True)  # auch alte Eintraege ohne Feld
        and e.get("content", "").strip()
    ]

    if not stale:
        return 0

    if max_summaries > 0:
        stale = stale[:max_summaries]

    updates = 0
    for entry in stale:
        related = entry.get("related_character", "")
        facts = entry.get("content", "")
        if not related or not facts:
            continue

        previous_summary = entry.get("summary", "")
        logger.info("Summarize %s -> %s (%d chars facts)", character_name, related, len(facts))
        summary = _generate_summary(character_name, related, facts, previous_summary)
        if summary:
            entry["summary"] = summary
            entry["summary_stale"] = False
            updates += 1
            logger.debug(
                "Summary fuer %s -> %s: %s",
                character_name, related, summary[:80]
            )

    if updates > 0:
        save_memories(character_name, entries)

    return updates


def _generate_summary(character_name: str,
    related_character: str,
    facts: str,
    previous_summary: str = "") -> Optional[str]:
    """Generiert eine narrative Beziehungs-Zusammenfassung per LLM.

    Die vorherige Summary wird als Kontext mitgegeben, damit Wissen aus
    bereits herausgefallenen Fakten (Sliding Window) nicht verloren geht.
    """
    from app.core.llm_router import llm_call

    previous_section = ""
    if previous_summary:
        previous_section = (
            f"\nBisherige Zusammenfassung der Beziehung:\n{previous_summary}\n"
            "Beruecksichtige diese als Basis und ergaenze/aktualisiere sie mit den neuen Fakten.\n"
        )

    system_prompt = (
        f"Du bist ein Beziehungs-Analyst fuer fiktive Characters.\n"
        f"Character: {character_name}\n"
        f"Anderer Character: {related_character}\n\n"
        "Erstelle eine kurze, narrative Zusammenfassung der Beziehung aus Sicht "
        f"von {character_name}. Die Summary soll:\n"
        "- Die Art der Beziehung beschreiben (Freund, Bekannter, Rivale, etc.)\n"
        "- Wichtige gemeinsame Erlebnisse/Interaktionen erwaehnen\n"
        "- Den emotionalen Ton der Beziehung einfangen\n"
        "- In 1-3 Saetzen formuliert sein\n"
        "- In der Sprache des Characters geschrieben sein (als wuerde der Character darueber nachdenken)\n\n"
        "Antworte NUR mit der Zusammenfassung, kein anderer Text."
    )

    user_msg = f"/no_think\n{previous_section}Aktuelle Fakten/Ereignisse:\n{facts}"

    try:
        response = llm_call(
            task="relationship_summary",
            system_prompt=system_prompt,
            user_prompt=user_msg,
            agent_name=character_name,
            label=f"{character_name}->{related_character}")

        if not response or not response.content:
            return None

        # Bereinigen
        summary = response.content.strip()
        summary = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', summary).strip()
        # Anfuehrungszeichen entfernen falls LLM die Summary in Quotes verpackt
        if summary.startswith('"') and summary.endswith('"'):
            summary = summary[1:-1].strip()

        return summary if summary else None

    except Exception as e:
        logger.error("LLM Summary Fehler fuer %s -> %s: %s", character_name, related_character, e)
        return None


def register_relationship_summary_handler():
    """Registriert den Handler in der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("relationship_summary", handle_relationship_summary)
    logger.info("Relationship Summary Handler registriert")

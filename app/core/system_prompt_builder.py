"""System prompt data loader (slim).

The original ``build_system_prompt`` + ``THOUGHT_FULL/REACTION`` template
composer has been removed — the AgentLoop builds its slim prompt via
``app/core/thought_context.py`` and ``chat/agent_thought.md`` instead.

This module now only provides:

- ``load_prompt_data(character_name, sections)`` — collects the heavy
  per-character context (personality, location, presence, events,
  memory, relationships, ...) as ready-to-render strings. Used by the
  rp_first dual-LLM tool-system context in ``thoughts.py``.

- ``build_recent_activity_section(...)`` — bullet list of what the
  character recently did, queried from ``state_history``. Used by the
  chat_stream prompt and the rp_first tool-system context.

- The ``IDENTITY / SITUATION / ...`` section sentinels are kept as
  string constants because callers pass them as a ``Set[str]`` to
  ``load_prompt_data`` to opt into specific data loads.
"""
from datetime import datetime
from typing import Any, Dict, Set

from app.core.log import get_logger

logger = get_logger("system_prompt_builder")


# ============================================================================
# Section labels — used by load_prompt_data() to opt into data loads.
# ============================================================================
IDENTITY = "identity"
TASK = "task"
ASSIGNMENTS = "assignments"
PENDING = "pending"
SITUATION = "situation"
PRESENCE = "presence"
EVENTS = "events"
MEMORY = "memory"
ARCS = "arcs"
RELATIONSHIPS = "relationships"
RULES_PRESENCE = "rules_presence"
INTENT = "intent"
RESPONSE_RULES = "response_rules"
RECENT_ACTIVITY = "recent_activity"

# Convenience: load everything (used by the rp_first tool-system builder).
THOUGHT_FULL: Set[str] = {
    IDENTITY, TASK, ASSIGNMENTS, PENDING, SITUATION, PRESENCE,
    EVENTS, MEMORY, ARCS, RULES_PRESENCE, INTENT, RESPONSE_RULES,
    RECENT_ACTIVITY,
}


# ============================================================================
# Data loader (loads only what's needed for the requested sections)
# ============================================================================

def load_prompt_data(character_name: str, sections: Set[str]) -> Dict[str, Any]:
    from app.models.character import (
        get_character_profile,
        get_character_current_location)
    from app.models.world import get_location_name

    profile = get_character_profile(character_name)
    data: Dict[str, Any] = {}

    data["personality"] = (profile.get("character_personality", "") or "").strip()
    data["task"] = (profile.get("character_task", "") or "").strip()

    location_id = profile.get("current_location", "")
    data["location_id"] = location_id
    data["location_name"] = get_location_name(location_id) if location_id else "Unknown"
    data["activity"] = profile.get("current_activity", "") or "None"
    data["feeling"] = profile.get("current_feeling", "") or "Neutral"
    data["time_of_day"] = datetime.now().strftime("%H:%M")

    if PRESENCE in sections:
        presence_lines, anyone_nearby = _load_presence(
            character_name, location_id)
        data["presence_lines"] = presence_lines
        data["anyone_nearby"] = anyone_nearby
        # Pre-rendered text block for callers that want a single string
        # (rp_first tool-system content in thoughts.py).
        data["nearby_hint"] = _format_presence_block(
            data["location_name"], presence_lines, anyone_nearby
        ) if presence_lines else ""

    if EVENTS in sections:
        data["events_section"] = _load_events(location_id)

    if MEMORY in sections:
        data["memory_section"] = _load_memory(character_name)

    if ARCS in sections:
        data["arc_context"] = _load_arcs(character_name)

    if ASSIGNMENTS in sections:
        data["assignment_section"] = _load_assignments(character_name)

    if PENDING in sections:
        data["pending_section"] = _load_pending(character_name)

    if RELATIONSHIPS in sections:
        data["relationships_section"] = _load_relationships(character_name)

    return data


def _format_presence_block(location_name: str, presence_lines: list,
                            anyone_nearby: bool) -> str:
    """Plain-text presence block (replaces former sections/presence.md)."""
    parts = [f"Present at '{location_name}':"]
    parts.extend(presence_lines)
    if anyone_nearby:
        parts.append("You can interact with present characters (TalkTo).")
        parts.append(
            "IMPORTANT: ONLY the people listed above are here. "
            "Do NOT invent further attendees.")
    else:
        parts.append("You are otherwise ALONE. NO other characters are here.")
        parts.append("Do NOT invent interactions with absent persons.")
    return "\n".join(parts)


def _load_presence(character_name: str, location_id: str) -> tuple:
    """Build ``presence_lines`` (list of bullet strings) and ``anyone_nearby``
    flag for the active world. Returns ([], False) when no location."""
    if not location_id:
        return [], False

    from app.models.character import (
        list_available_characters,
        get_character_current_location,
        get_character_current_activity)
    from app.models.account import get_active_character

    nearby = []
    for other in list_available_characters():
        if other == character_name:
            continue
        other_loc = get_character_current_location(other)
        if other_loc and other_loc == location_id:
            nearby.append(other)

    player_char = get_active_character()
    player_loc = get_character_current_location(player_char) if player_char else ""
    player_is_here = bool(player_loc and player_loc == location_id)

    lines: list = []
    if player_char and player_is_here:
        lines.append(f"- {player_char} is present")
    elif player_char:
        lines.append(
            f"- {player_char} is NOT here "
            f"(do NOT react as if {player_char} were present, "
            f"do NOT imagine an interaction with {player_char})"
        )

    for other in nearby:
        other_act = get_character_current_activity(other) or ""
        suffix = f" ({other_act})" if other_act else ""
        lines.append(f"- {other} is here{suffix}")

    return lines, bool(nearby)


def _load_events(location_id: str) -> str:
    if not location_id:
        return ""
    try:
        from app.models.events import build_events_prompt_section
        return build_events_prompt_section(location_id=location_id) or ""
    except Exception as e:
        logger.debug("Events laden fehlgeschlagen: %s", e)
    return ""


def _load_memory(character_name: str) -> str:
    try:
        from app.models.memory import build_memory_prompt_section
        return build_memory_prompt_section(character_name, user_name="", current_message="") or ""
    except Exception as e:
        logger.debug("Memory laden fehlgeschlagen: %s", e)
    return ""


def _load_arcs(character_name: str) -> str:
    try:
        from app.core.story_engine import get_story_engine
        return get_story_engine().inject_arc_context(character_name) or ""
    except Exception as e:
        logger.debug("Arc-Kontext nicht verfuegbar: %s", e)
    return ""


def _load_assignments(character_name: str) -> str:
    try:
        from app.models.assignments import build_assignment_prompt_section
        return build_assignment_prompt_section(character_name) or ""
    except Exception as e:
        logger.debug("Assignment-Section laden fehlgeschlagen: %s", e)
    return ""


def _load_pending(character_name: str) -> str:
    try:
        from app.core.pending_reports import build_prompt_section
        return build_prompt_section(character_name) or ""
    except Exception as e:
        logger.debug("Pending-Reports laden fehlgeschlagen: %s", e)
    return ""


def _load_relationships(character_name: str) -> str:
    try:
        from app.models.relationship import build_relationship_prompt_section
        return build_relationship_prompt_section(character_name) or ""
    except Exception as e:
        logger.debug("Relationships laden fehlgeschlagen: %s", e)
    return ""


# ============================================================================
# Recent Activity — rendered as a self-contained block (no template needed)
# ============================================================================

_RECENT_WINDOW_HOURS = 6
_RECENT_MAX_ENTRIES = 24


def _time_str(ts: str) -> str:
    """'HH:MM' from ISO string, empty on error."""
    try:
        return ts[11:16]
    except Exception:
        return ""


def _resolve_location_name(loc_id: str) -> str:
    if not loc_id:
        return ""
    try:
        from app.models.world import get_location_name
        name = get_location_name(loc_id)
        if name and name != loc_id:
            return name
    except Exception:
        pass
    return loc_id


def build_recent_activity_section(character_name: str,
                                   hours: int = _RECENT_WINDOW_HOURS,
                                   max_entries: int = _RECENT_MAX_ENTRIES) -> str:
    """Build the "## Recently experienced" block from state_history."""
    try:
        from datetime import timedelta
        from app.core.db import get_connection
        import json as _json

        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = get_connection()
        rows = conn.execute(
            "SELECT state_json FROM state_history "
            "WHERE character_name=? AND ts>=? ORDER BY ts ASC",
            (character_name, cutoff),
        ).fetchall()
        if not rows:
            return ""

        events: list = []
        for (sj,) in rows:
            try:
                d = _json.loads(sj or "{}")
            except Exception:
                continue
            t = d.get("type") or ""
            if t == "effects":
                continue
            val = (d.get("value") or "").strip()
            if not val:
                continue
            meta = d.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            ts = d.get("timestamp") or ""
            entry = {"ts": ts, "type": t, "value": val,
                     "partner": (meta.get("partner") or "").strip(),
                     "reason": (meta.get("reason") or "").strip(),
                     "detail": (meta.get("detail") or "").strip()}
            if t == "location":
                entry["value_display"] = _resolve_location_name(val)
            elif t == "access_denied":
                entry["value_display"] = val
            else:
                entry["value_display"] = val
            events.append(entry)

        if not events:
            return ""

        # Aggregation: collapse adjacent duplicates
        collapsed: list = []
        for e in events:
            if collapsed:
                last = collapsed[-1]
                if last["type"] == e["type"] and last["value"] == e["value"]:
                    last["end_ts"] = e["ts"]
                    if e.get("partner") and not last.get("partner"):
                        last["partner"] = e["partner"]
                    continue
            collapsed.append(dict(e, end_ts=e["ts"]))

        collapsed = collapsed[-max_entries:]

        lines: list = []
        for e in collapsed:
            start = _time_str(e["ts"])
            end = _time_str(e.get("end_ts") or "")
            if end and end != start:
                time_str = f"{start}-{end}"
            else:
                time_str = start
            t = e["type"]
            val = e["value_display"] or e["value"]
            if t == "location":
                lines.append(f"• {time_str}  → {val}")
            elif t == "activity":
                suffix = f" (with {e['partner']})" if e.get("partner") else ""
                if e.get("detail"):
                    suffix += f" — {e['detail'][:60]}"
                lines.append(f"• {time_str}  {val}{suffix}")
            elif t == "access_denied":
                reason_raw = (e.get("reason") or "").strip().rstrip(".")
                default_reason = reason_raw.lower() in ("", "zugang verweigert", "access denied")
                reason = "" if default_reason else f" — {reason_raw}"
                lines.append(f"• {time_str}  Wanted to go to {val}, access denied{reason}")
            else:
                lines.append(f"• {time_str}  {t}: {val}")

        if not lines:
            return ""

        header = f"## Recently experienced (last {hours}h):"
        return header + "\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("build_recent_activity_section fehlgeschlagen: %s", e)
        return ""

"""Zentraler System-Prompt-Builder fuer LLM-Aufrufe.

Baut den System-Prompt aus einzelnen Sektionen zusammen. Jede Sektion
kann per ``sections``-Set aktiviert oder deaktiviert werden.

Verwendung::

    from app.core.system_prompt_builder import (
        build_system_prompt, THOUGHT_FULL, THOUGHT_REACTION)

    # Voller Thought-Prompt (alle Sektionen)
    prompt = build_system_prompt(name, sections=THOUGHT_FULL)

    # Minimaler Prompt fuer Forced-Reactions (z.B. Instagram-Kommentar)
    prompt = build_system_prompt(name,
        sections=THOUGHT_REACTION,
        context_hint="Du siehst einen neuen Post...",
        tool_whitelist=["InstagramComment"])
"""
from datetime import datetime
from typing import Any, Dict, Optional, Set

from app.core.log import get_logger

logger = get_logger("system_prompt_builder")


# ============================================================================
# Section-Konstanten
# ============================================================================

IDENTITY = "identity"              # Name, Persoenlichkeit
TASK = "task"                      # character_task
ASSIGNMENTS = "assignments"        # Aktive temporaere Aufgaben
PENDING = "pending"                # Offene Rueckmeldungen
SITUATION = "situation"            # Ort, Aktivitaet, Stimmung, Uhrzeit
PRESENCE = "presence"              # Anwesende am Ort + Abwesenheits-Warnungen
EVENTS = "events"                  # Ereignisse am Ort
MEMORY = "memory"                  # Langzeit-Erinnerungen
ARCS = "arcs"                      # Story-Arc-Kontext
RELATIONSHIPS = "relationships"    # Beziehungen zu relevanten Characters
RULES_PRESENCE = "rules_presence"  # Strikte Anti-Halluzinations-Regeln
INTENT = "intent"                  # Intent-Tracking-Instruktionen
RESPONSE_RULES = "response_rules"  # "WICHTIG fuer deine Antwort"
RECENT_ACTIVITY = "recent_activity"  # Was der Character in den letzten Stunden tat

# ============================================================================
# Presets
# ============================================================================

THOUGHT_FULL: Set[str] = {
    IDENTITY, TASK, ASSIGNMENTS, PENDING, SITUATION, PRESENCE,
    EVENTS, MEMORY, ARCS, RULES_PRESENCE, INTENT, RESPONSE_RULES,
    RECENT_ACTIVITY,
}

THOUGHT_REACTION: Set[str] = {
    IDENTITY, SITUATION, RELATIONSHIPS, RESPONSE_RULES,
}


# ============================================================================
# Builder
# ============================================================================

def build_system_prompt(character_name: str,
    *,
    sections: Optional[Set[str]] = None,
    context_hint: str = "",
    tool_whitelist: Optional[list] = None,
    tools_hint: str = "",
    medium: Optional[str] = None) -> str:
    """Baut einen System-Prompt aus den angeforderten Sektionen zusammen.

    Args:
        character_name: Name des Characters
        sections: Set der gewuenschten Sektionen (None = THOUGHT_FULL)
        context_hint: Dringender Kontext fuer forcierte Gedanken
        tool_whitelist: Erlaubte Tool-Namen (fuer Hint-Section)
        tools_hint: Tool-Verfuegbarkeits-Hinweis (Single-Modus)
        medium: Kommunikations-Kontext fuer die SITUATION-Section —
            "in_person" (face-to-face), "messaging", "telegram", "instagram".
            None = kein Medium-Hinweis (freier Gedanke, keine laufende
            Kommunikation). Beeinflusst Stil-Erwartung bei talk_to/send_message.
    """
    if sections is None:
        sections = THOUGHT_FULL

    data = load_prompt_data(character_name, sections)
    parts: list = []

    # Context-Hint (forcierte Gedanken) — immer am Anfang wenn vorhanden
    if context_hint:
        parts.append(_build_hint_section(context_hint, tool_whitelist))

    if IDENTITY in sections:
        parts.append(_build_identity(character_name, data.get("personality", "")))

    if PENDING in sections and data.get("pending_section"):
        parts.append(data["pending_section"])

    if ASSIGNMENTS in sections and data.get("assignment_section"):
        parts.append(data["assignment_section"])

    if TASK in sections and data.get("task"):
        parts.append(f"Deine Aufgabe: {data['task']}")

    if SITUATION in sections:
        parts.append(_build_situation(data, medium=medium))

    if PRESENCE in sections and data.get("nearby_hint"):
        parts.append(data["nearby_hint"])

    if EVENTS in sections and data.get("events_section"):
        parts.append(data["events_section"])

    if RECENT_ACTIVITY in sections:
        _rec = build_recent_activity_section(character_name)
        if _rec:
            parts.append(_rec)

    if MEMORY in sections and data.get("memory_section"):
        parts.append(data["memory_section"])

    if ARCS in sections and data.get("arc_context"):
        parts.append(data["arc_context"])

    if RELATIONSHIPS in sections and data.get("relationships_section"):
        parts.append(data["relationships_section"])

    if RULES_PRESENCE in sections:
        parts.append(_build_rules(data.get("location_name", "Unbekannt")))

    # Entscheidungs-Prompt
    parts.append(_build_decision_prompt(tools_hint=tools_hint))

    if INTENT in sections:
        parts.append(_build_intent_section())

    if RESPONSE_RULES in sections:
        _has_assignments = bool(ASSIGNMENTS in sections and data.get("assignment_section"))
        parts.append(_build_response_rules(has_assignments=_has_assignments))

    return "\n\n".join(p for p in parts if p and p.strip())


# ============================================================================
# Daten-Loader (laedt nur was gebraucht wird)
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
    data["location_name"] = get_location_name(location_id) if location_id else "Unbekannt"
    data["activity"] = profile.get("current_activity", "") or "Keine"
    data["feeling"] = profile.get("current_feeling", "") or "Neutral"
    data["time_of_day"] = datetime.now().strftime("%H:%M")

    if PRESENCE in sections:
        data["nearby_hint"] = _build_presence(character_name, location_id, data["location_name"])

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


# ============================================================================
# Section-Builder
# ============================================================================

def _build_hint_section(context_hint: str, tool_whitelist: Optional[list]) -> str:
    if tool_whitelist:
        return (
            f"# DRINGENDER KONTEXT FUER DIESEN GEDANKEN\n"
            f"{context_hint}\n"
            f"-> Die EINZIG erlaubte Aktion ist ein Aufruf von: "
            f"{', '.join(tool_whitelist)}.\n"
            f"-> Erzeuge KEINE Chat-Nachricht, KEINEN Monolog, KEINEN neuen Post. "
            f"Wenn du nichts beitragen willst, antworte nur mit: SKIP."
        )
    return (
        f"# DRINGENDER KONTEXT FUER DIESEN GEDANKEN\n"
        f"{context_hint}\n"
        f"-> Reagiere DIREKT auf diesen Kontext. Nutze die passenden Tools "
        f"(z.B. SendMessage / TalkTo) um die offenen Rueckmeldungen zu erledigen."
    )


def _build_identity(character_name: str, personality: str) -> str:
    lines = [f"Du bist {character_name}."]
    if personality:
        lines.append(f"Persoenlichkeit: {personality}")
    return "\n".join(lines)


_MEDIUM_LABEL = {
    "in_person": "face-to-face (am gleichen Ort)",
    "messaging": "per Chat-Nachricht (nicht vor Ort)",
    "telegram": "per Telegram (nicht vor Ort)",
    "instagram": "via Instagram-Kommentar",
}


def _build_situation(data: Dict[str, Any], medium: Optional[str] = None) -> str:
    lines = [
        "Aktuelle Situation:",
        f"- Ort: {data.get('location_name', 'Unbekannt')}",
        f"- Aktivitaet: {data.get('activity', 'Keine')}",
        f"- Stimmung: {data.get('feeling', 'Neutral')}",
        f"- Uhrzeit: {data.get('time_of_day', '--:--')}",
    ]
    if medium:
        label = _MEDIUM_LABEL.get(medium, medium)
        lines.append(f"- Kommunikations-Kontext: {label}")
    return "\n".join(lines)


def _build_presence(character_name: str, location_id: str, location_name: str) -> str:
    from app.models.character import (
        list_available_characters,
        get_character_current_location,
        get_character_current_activity)
    from app.models.account import get_active_character

    nearby = []
    if location_id:
        for other in list_available_characters():
            if other == character_name:
                continue
            other_loc = get_character_current_location(other)
            if other_loc and other_loc == location_id:
                nearby.append(other)

    player_char = get_active_character()
    player_loc = get_character_current_location(player_char) if player_char else ""
    player_is_here = bool(player_loc and player_loc == location_id)

    presence_lines = []
    if player_char and player_is_here:
        presence_lines.append(f"- {player_char} ist anwesend")
    elif player_char:
        presence_lines.append(
            f"- {player_char} ist NICHT hier "
            f"(reagiere NICHT so als waere {player_char} anwesend, "
            f"stelle dir KEINE Interaktion mit {player_char} vor)"
        )

    for other in nearby:
        other_act = get_character_current_activity(other) or ""
        suffix = f" ({other_act})" if other_act else ""
        presence_lines.append(f"- {other} ist hier{suffix}")

    if nearby:
        return (
            f"Anwesende am Ort '{location_name}':\n"
            + "\n".join(presence_lines) + "\n"
            f"Du kannst mit anwesenden Characters interagieren (TalkTo).\n"
            f"WICHTIG: NUR die oben genannten Personen sind hier. "
            f"Erfinde KEINE weiteren Anwesenden."
        )
    return (
        f"Anwesende am Ort '{location_name}':\n"
        + "\n".join(presence_lines) + "\n"
        f"Du bist ansonsten ALLEIN. Es sind KEINE weiteren Characters hier.\n"
        f"Erfinde KEINE Interaktionen mit abwesenden Personen."
    )


def _build_rules(location_name: str) -> str:
    return (
        f"STRIKTE REGELN:\n"
        f"1. Deine Nachricht MUSS zur aktuellen Umgebung und dem Ort '{location_name}' passen.\n"
        f"2. Erwaehne oder interagiere NUR mit Personen die oben als 'anwesend' aufgelistet sind.\n"
        f"3. Wenn du allein bist, beschreibe deine eigenen Gedanken, Aktivitaeten oder Beobachtungen.\n"
        f"4. Erfinde KEINE Dialoge oder Reaktionen von abwesenden Personen.\n"
        f"5. Auch wenn du dich an andere Characters ERINNERST — sie sind NICHT hier, "
        f"es sei denn sie stehen in der Anwesendenliste."
    )


def _build_decision_prompt(tools_hint: str = "") -> str:
    lines = [
        "Entscheide basierend auf deiner Aufgabe und Situation: Gibt es etwas, "
        "das du jetzt tun moechtest? Wenn andere Characters in der Naehe sind, "
        "ueberlege ob du mit ihnen interagieren moechtest."
    ]
    if tools_hint:
        lines.append(tools_hint)
    return "\n".join(lines)


def _build_intent_section() -> str:
    return (
        "Intent tracking: If you commit to a concrete real-world action (posting something, "
        "sending a message, doing something at a specific time), add this marker at the END "
        "of your response on a new line:\n"
        "[INTENT: <type> | delay=<0/30m/2h/1d> | key=value]\n"
        "Types: instagram_post, send_message, remind, execute_tool\n"
        "Delay: 0=now, 30m, 2h, 1d, or 14:00 (time of day)\n"
        "Only add this when genuinely committing to a specific action. "
        "Do NOT add for hypothetical, past, or uncertain actions."
    )


def _build_response_rules(has_assignments: bool = False) -> str:
    lines = [
        "WICHTIG fuer deine Antwort:",
        "- Schreibe NUR narrativen Text (was du denkst, fuehlst, beobachtest)",
        "- Schreibe KEINE Tool-Namen, Befehle oder [ToolName](...) in deinen Text",
        "- Tool-Aufrufe passieren automatisch im Hintergrund, nicht in deiner Antwort",
    ]
    if has_assignments:
        lines.append(
            "- Du hast AKTIVE AUFGABEN — nutze deine Tools um sie zu erfuellen! "
            "Fotos machen = ImageGeneration benutzen, Recherche = WebSearch benutzen. "
            "Beschreibe nicht nur was du tust, FUEHRE ES AUS!"
        )
    lines.append("- Wenn es nichts Relevantes gibt, antworte nur mit: SKIP")
    return "\n".join(lines)


# ============================================================================
# Daten-Loader (mit Fehlerbehandlung)
# ============================================================================

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
# Recent Activity — was hat der Character in den letzten Stunden getan
# ============================================================================

_RECENT_WINDOW_HOURS = 6
_RECENT_MAX_ENTRIES = 24


def _time_str(ts: str) -> str:
    """'HH:MM' aus ISO-String, leer bei Fehler."""
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
    """Baut "## Kuerzlich erlebt"-Block aus state_history.

    Aggregationsregeln:
    - effects-Eintraege werden ignoriert (interne Status-Ticks)
    - Adjacente identische Activities zusammenfassen (Start-Ende-Range)
    - Adjacente access_denied auf gleiche Location zusammenfassen
    - Location-IDs → Namen
    - Partner (aus metadata.partner) wird angehaengt
    - Output: prose-nahe Bullet-Liste, deutsch
    """
    try:
        from datetime import datetime, timedelta
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

        events: list = []  # {ts, type, value, partner, reason, location_name}
        for (sj,) in rows:
            try:
                d = _json.loads(sj or "{}")
            except Exception:
                continue
            t = d.get("type") or ""
            if t == "effects":
                continue  # Rauschen aus Zeit-Ticks
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
                # value ist bereits Location-Name laut record_access_denied
                entry["value_display"] = val
            else:
                entry["value_display"] = val
            events.append(entry)

        if not events:
            return ""

        # Aggregation: adjacente Duplikate zusammenfassen
        # (same type+value → Endzeit aktualisieren statt neu)
        collapsed: list = []
        for e in events:
            if collapsed:
                last = collapsed[-1]
                if last["type"] == e["type"] and last["value"] == e["value"]:
                    last["end_ts"] = e["ts"]
                    # Wenn partner beim spaeteren Eintrag gesetzt ist, uebernehmen
                    if e.get("partner") and not last.get("partner"):
                        last["partner"] = e["partner"]
                    continue
            collapsed.append(dict(e, end_ts=e["ts"]))

        # Letzte N Eintraege behalten
        collapsed = collapsed[-max_entries:]

        # Rendering — Bullet-Liste mit Zeitangaben
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
                suffix = f" (mit {e['partner']})" if e.get("partner") else ""
                if e.get("detail"):
                    suffix += f" — {e['detail'][:60]}"
                lines.append(f"• {time_str}  {val}{suffix}")
            elif t == "access_denied":
                reason_raw = (e.get("reason") or "").strip().rstrip(".")
                # Default-Reason "Zugang verweigert" nicht doppelt anhaengen
                default_reason = reason_raw.lower() in ("", "zugang verweigert")
                reason = "" if default_reason else f" — {reason_raw}"
                lines.append(f"• {time_str}  Wolltest zu {val}, Zugang verweigert{reason}")
            else:
                lines.append(f"• {time_str}  {t}: {val}")

        if not lines:
            return ""

        header = f"## Kuerzlich erlebt (letzte {hours}h):"
        return header + "\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("build_recent_activity_section fehlgeschlagen: %s", e)
        return ""

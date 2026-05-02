"""Activity Engine - Condition-Auswertung, Visibility, Cooldowns, Duration, Triggers.

Zentrale Logik fuer erweiterte Spezial-Aktivitaeten.
Wird von set_activity_skill.py und chat.py genutzt.
"""
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.log import get_logger

logger = get_logger("activity_engine")


# ============================================================
# 1. CONDITION PARSER & EVALUATOR
# ============================================================

def evaluate_condition(
    condition: str, character_name: str,
    location_id: str = "",
    room_id: str = "") -> Tuple[bool, str]:
    """Wertet eine Condition-String aus.

    Returns:
        (passed: bool, reason: str) - reason ist leer wenn passed=True
    """
    global _last_matched_partner
    _last_matched_partner = None

    if not condition or not condition.strip():
        return True, ""

    # OR-verknuepfte Gruppen zuerst splitten, dann AND innerhalb jeder Gruppe
    # Beispiel: "alone AND night OR alone AND day" → Gruppe1: alone AND night, Gruppe2: alone AND day
    or_groups = [g.strip() for g in re.split(r"\s+OR\s+", condition, flags=re.IGNORECASE)]

    last_reason = ""
    for group in or_groups:
        # AND-verknuepfte Bedingungen innerhalb einer Gruppe
        and_parts = [p.strip() for p in re.split(r"\s+AND\s+", group, flags=re.IGNORECASE)]
        group_passed = True
        group_reason = ""
        for part in and_parts:
            if not part:
                continue
            passed, reason = _evaluate_single_condition(part, character_name, location_id, room_id)
            if not passed:
                group_passed = False
                group_reason = reason
                break
        if group_passed:
            return True, ""
        last_reason = group_reason

    # Keine OR-Gruppe hat bestanden
    return False, last_reason


def _evaluate_single_condition(
    condition: str, character_name: str,
    location_id: str, room_id: str = "") -> Tuple[bool, str]:
    """Wertet eine einzelne Condition aus."""
    cond = condition.strip()

    # --- NOT prefix ---
    negated = False
    if cond.upper().startswith("NOT "):
        negated = True
        cond = cond[4:].strip()

    cond = cond.lower()

    passed, reason = _evaluate_single_condition_inner(cond, character_name, location_id, room_id)
    if negated:
        if passed:
            return False, f"Bedingung 'NOT {cond}' nicht erfuellt"
        return True, ""
    return passed, reason


def _evaluate_single_condition_inner(
    cond: str, character_name: str,
    location_id: str, room_id: str = "") -> Tuple[bool, str]:
    """Wertet eine einzelne Condition aus (ohne NOT-Handling)."""

    # --- alone ---
    if cond == "alone":
        return _check_alone(character_name, location_id)

    # --- night / day ---
    if cond == "night":
        hour = datetime.now().hour
        if hour >= 18 or hour < 6:
            return True, ""
        return False, "Nur nachts verfuegbar (18:00-06:00)"

    if cond == "day":
        hour = datetime.now().hour
        if 6 <= hour < 18:
            return True, ""
        return False, "Nur tagsueber verfuegbar (06:00-18:00)"

    # --- mood:X ---
    mood_match = re.match(r"mood:(.+)", cond)
    if mood_match:
        target_mood = mood_match.group(1).strip()
        return _check_mood(character_name, target_mood)

    # --- relationship:Name>N or relationship:any>N ---
    rel_match = re.match(r"relationship:(\w+)([><=])(\d+)", cond)
    if rel_match:
        target_char = rel_match.group(1)
        operator = rel_match.group(2)
        threshold = int(rel_match.group(3))
        return _check_relationship(character_name, location_id,
                                   target_char, operator, threshold, field="strength")

    # --- romantic:Name>N or romantic:any>N ---
    rom_match = re.match(r"romantic:(\w+)([><=])(\d+)", cond)
    if rom_match:
        target_char = rom_match.group(1)
        operator = rom_match.group(2)
        threshold = int(rom_match.group(3))
        return _check_relationship(character_name, location_id,
                                   target_char, operator, threshold, field="romantic")

    # --- {stat}>N / {stat}<N / {stat}=N — generisch fuer beliebige im Template
    # definierten Stat-Felder (store=status_effects). Reihenfolge: NACH den
    # spezifischen Praefixen (mood:, relationship:, etc.), sonst wuerde dies
    # "condition:drunk" o.ae. fehl-matchen.
    stat_match = re.match(r"([a-z_]+)([><=])(\d+)$", cond)
    if stat_match:
        stat_name = stat_match.group(1)
        # Reservierte Namen ausschliessen (haben eigene Handler oben)
        if stat_name not in ("mood", "relationship", "romantic", "has_item", "condition",
                             "event_active", "has_secret", "alone", "night", "day", "npc_present",
                             "current_activity"):
            operator = stat_match.group(2)
            threshold = int(stat_match.group(3))
            return _check_status_effect(character_name, stat_name, operator, threshold)

    # --- current_activity:X (aktuelle Activity des Characters) ---
    cact_match = re.match(r"current_activity:(.+)", cond)
    if cact_match:
        target = cact_match.group(1).strip().lower()
        try:
            from app.models.character import get_character_current_activity
            current = (get_character_current_activity(character_name) or "").lower()
            if not current:
                return False, f"Aktuelle Aktivitaet muss '{target}' sein (keine aktiv)"
            # Alle Namensvarianten der Target-Activity einsammeln fuer Match
            from app.models.activity_library import (
                get_library_activity,
                find_library_activity_by_name)
            names = {target}
            lib = get_library_activity(target) or find_library_activity_by_name(target)
            if lib:
                for k in ("id", "name", "name_de", "name_en"):
                    v = (lib.get(k) or "").strip().lower()
                    if v:
                        names.add(v)
            if current in names or any(n and n in current for n in names):
                return True, ""
            return False, f"Aktuelle Aktivitaet muss '{target}' sein (ist: '{current}')"
        except Exception as e:
            logger.debug("current_activity-Check fehlgeschlagen: %s", e)
            return False, f"Aktuelle Aktivitaet muss '{target}' sein"

    # --- condition:X (active_conditions im Profil) ---
    cond_match = re.match(r"condition:(.+)", cond)
    if cond_match:
        cond_name = cond_match.group(1).strip().lower()
        try:
            from app.models.character import get_character_profile
            profile = get_character_profile(character_name) or {}
            active = profile.get("active_conditions", []) or []
            if any((c.get("name") or "").lower() == cond_name for c in active):
                return True, ""
            return False, f"Zustand nicht aktiv: {cond_name}"
        except Exception:
            return False, f"Zustand nicht aktiv: {cond_name}"

    # --- has_item:<id|name> --- (Character-Inventar)
    item_match = re.match(r"has_item:(.+)", cond)
    if item_match:
        token = item_match.group(1).strip()
        try:
            from app.models.inventory import (
                has_item as _has_item,
                resolve_item_id as _resolve_item_id,
                get_item as _get_item)
            resolved_id = _resolve_item_id(token) or token
            if _has_item(character_name, resolved_id):
                return True, ""
            item_name = token
            try:
                _it = _get_item(resolved_id)
                if _it:
                    item_name = _it.get("name", token)
            except Exception:
                pass
            return False, f"Benoetigt Item: {item_name}"
        except Exception:
            return True, ""

    # --- room_has_item:<id|name> --- (Ziel-Raum enthaelt Item)
    room_item_match = re.match(r"room_has_item:(.+)", cond)
    if room_item_match:
        token = room_item_match.group(1).strip()
        if not room_id:
            return False, "Kein Raum-Kontext"
        try:
            from app.models.inventory import (
                resolve_item_id as _resolve_item_id,
                get_item as _get_item,
                get_room_items as _get_room_items)
            resolved_id = _resolve_item_id(token) or token
            room_items = _get_room_items(location_id, room_id) or []
            for ri in room_items:
                if ri.get("item_id") == resolved_id:
                    return True, ""
                # Name-Match fallback
                it = _get_item(ri.get("item_id", ""))
                if it and (it.get("name") or "").strip().lower() == token.lower():
                    return True, ""
            item_name = token
            try:
                _it = _get_item(resolved_id)
                if _it:
                    item_name = _it.get("name", token)
            except Exception:
                pass
            return False, f"Raum enthaelt kein Item: {item_name}"
        except Exception:
            return True, ""

    # --- has_secret ---
    if cond == "has_secret":
        return _check_has_secret(character_name)

    # --- npc_present ---
    if cond == "npc_present":
        # Placeholder fuer NPC-System
        return True, ""

    # --- event_active:category ---
    event_match = re.match(r"event_active:(.+)", cond)
    if event_match:
        # Placeholder fuer Event-Kategorien
        return True, ""

    # Unbekannte Condition: durchlassen mit Warnung
    logger.warning("Unbekannte Condition: '%s'", cond)
    return True, ""


def _check_alone(character_name: str, location_id: str) -> Tuple[bool, str]:
    """Prueft ob der Character allein an der Location ist."""
    if not location_id:
        return True, ""
    try:
        from app.models.character import list_available_characters, get_character_current_location
        all_chars = list_available_characters()
        others_at_loc = [
            c for c in all_chars
            if c != character_name and get_character_current_location(c) == location_id
        ]
        if others_at_loc:
            return False, f"Nicht allein (auch hier: {', '.join(others_at_loc)})"
        return True, ""
    except Exception as e:
        logger.warning("alone-Check fehlgeschlagen: %s", e)
        return True, ""


def _check_mood(character_name: str, target_mood: str) -> Tuple[bool, str]:
    """Prueft ob der aktuelle Mood dem Ziel entspricht."""
    try:
        from app.models.character import get_character_current_feeling
        current = (get_character_current_feeling(character_name) or "").lower()
        if target_mood.lower() in current or current in target_mood.lower():
            return True, ""
        return False, f"Mood-Voraussetzung: {target_mood} (aktuell: {current or 'unbekannt'})"
    except Exception:
        return True, ""


def _check_relationship(character_name: str, location_id: str,
    target_char: str, operator: str, threshold: int,
    field: str = "strength") -> Tuple[bool, str]:
    """Prueft eine Beziehungs-Bedingung.

    - target_char kann ein Name oder "any" sein
    - Prueft ob der Ziel-Character am gleichen Ort ist
    - field: "strength" (Freundschaft 0-100) oder "romantic" (romantic_tension 0-100, intern 0-1)
    - Bei Erfolg wird der gematchte Partner-Name in _last_matched_partner gespeichert
    """
    global _last_matched_partner
    try:
        from app.models.character import list_available_characters, get_character_current_location
        from app.models.relationship import get_relationship

        label = "Romantik" if field == "romantic" else "Beziehung"

        # Kandidaten bestimmen
        if target_char.lower() == "any":
            all_chars = list_available_characters()
            candidates = [
                c for c in all_chars
                if c != character_name
                and get_character_current_location(c) == location_id
            ]
            if not candidates:
                return False, f"Kein anderer Character am gleichen Ort"
        else:
            # Konkreter Name: muss am gleichen Ort sein
            char_loc = get_character_current_location(target_char)
            if char_loc != location_id:
                return False, f"{target_char} ist nicht am gleichen Ort"
            candidates = [target_char]

        for cand in candidates:
            rel = get_relationship(character_name, cand)
            if not rel:
                continue
            if field == "romantic":
                value = int((rel.get("romantic_tension", 0)) * 100)
            else:
                value = int(rel.get("strength", 0))
            if _compare(value, operator, threshold):
                _last_matched_partner = cand
                return True, ""

        if target_char.lower() == "any":
            return False, f"Kein Character am Ort mit {label} {operator}{threshold}"
        return False, f"{label} zu {target_char}: Bedingung {operator}{threshold} nicht erfuellt"
    except Exception as e:
        logger.debug("Relationship check failed: %s", e)
        return True, ""


def _compare(value: int, operator: str, threshold: int) -> bool:
    if operator == ">":
        return value > threshold
    if operator == "<":
        return value < threshold
    if operator == "=":
        return value == threshold
    return False


# Letzter gematchter Partner (fuer partner_activity)
_last_matched_partner: Optional[str] = None


def get_last_matched_partner() -> Optional[str]:
    """Gibt den zuletzt durch eine Relationship-Condition gematchten Partner zurueck."""
    return _last_matched_partner


def check_partner_available(character_name: str,
    location_id: str = "") -> Tuple[bool, str]:
    """Prueft ob mindestens ein anderer Character am gleichen Ort ist.

    Returns:
        (available: bool, reason: str) - reason ist leer wenn available=True
    """
    if not location_id:
        return False, "Kein Standort gesetzt"
    try:
        from app.models.character import list_available_characters, get_character_current_location
        all_chars = list_available_characters()
        others_at_loc = [
            c for c in all_chars
            if c != character_name and get_character_current_location(c) == location_id
        ]
        if others_at_loc:
            return True, ""
        return False, "Kein anderer Character am gleichen Ort"
    except Exception as e:
        logger.warning("Partner-Check fehlgeschlagen: %s", e)
        return True, ""


def _check_status_effect(character_name: str,
    stat_name: str, operator: str, threshold: int) -> Tuple[bool, str]:
    """Prueft einen Status-Wert generisch gegen profile.status_effects.

    Fehlt der Stat-Wert komplett, wird 100 angenommen (Character startet voll).
    """
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name)
        status = profile.get("status_effects", {})
        value = status.get(stat_name, 100)
        if operator == ">" and value > threshold:
            return True, ""
        if operator == "<" and value < threshold:
            return True, ""
        if operator == "=" and int(value) == threshold:
            return True, ""
        return False, f"{stat_name}: {int(value)} (braucht {operator}{threshold})"
    except Exception:
        return True, ""


def _check_has_secret(character_name: str) -> Tuple[bool, str]:
    """Prueft ob der Character Geheimnisse hat."""
    try:
        from app.models.secrets import list_secrets
        secrets = list_secrets(character_name)
        if secrets:
            return True, ""
        return False, "Keine Geheimnisse vorhanden"
    except Exception:
        return True, ""


# ============================================================
# 2. VISIBILITY RESOLVER
# ============================================================

def resolve_activity_visibility(acting_character: str,
    observing_character: str,
    activity_name: str,
    same_location: bool = False) -> str:
    """Gibt die sichtbare Aktivitaet zurueck, basierend auf Visibility-Einstellung.

    Fuer den eigenen Character oder wenn kein Observer: immer die echte Aktivitaet.
    Fuer andere: abhaengig von visibility-Feld der Spezial-Aktivitaet.

    Args:
        same_location: Wenn True, sind beide am selben Ort (z.B. Gruppenchat).
            In dem Fall ist die Aktivitaet immer sichtbar — man kann sehen
            was jemand neben einem tut.
    """
    if acting_character == observing_character or not observing_character:
        return activity_name

    # Spezial-Aktivitaet finden
    sa = _find_activity_definition(acting_character, activity_name)
    if not sa:
        return activity_name  # Normale Aktivitaet — immer sichtbar

    visibility = sa.get("visibility", "visible")

    if visibility == "visible":
        return activity_name
    elif visibility == "hidden":
        # Am selben Ort: man sieht, dass der Character etwas tut — aber nicht was.
        # Entfernt: nicht sichtbar als Aktivitaet.
        if same_location:
            return "ist beschaeftigt"
        return "ist beschaeftigt"
    elif visibility == "disguised":
        # Disguise gilt AUCH am selben Ort — das ist der Sinn der Tarnung.
        # Beobachter sehen den disguise_text statt der echten Aktivitaet.
        return sa.get("disguise_text", "ist beschaeftigt")
    else:
        return activity_name


def _find_activity_definition(character_name: str, activity_name: str) -> Optional[Dict[str, Any]]:
    """Findet eine Aktivitaet nach Name in der Bibliothek."""
    try:
        from app.models.activity_library import get_library_activity, find_library_activity_by_name
        act = get_library_activity(activity_name)
        if not act:
            act = find_library_activity_by_name(activity_name)
        return act
    except Exception:
        return None


# ============================================================
# 3. COOLDOWN CHECK
# ============================================================

def check_cooldown(character_name: str,
    activity_name: str) -> Tuple[bool, str]:
    """Prueft ob eine Aktivitaet auf Cooldown ist.

    Returns:
        (available: bool, message: str)
    """
    sa = _find_activity_definition(character_name, activity_name)
    if not sa:
        return True, ""

    cooldown_hours = sa.get("cooldown_hours", 0)
    if not cooldown_hours or cooldown_hours <= 0:
        return True, ""

    # Cooldown-Timestamp aus Character-Profil lesen
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name)
        cooldowns = profile.get("activity_cooldowns", {})
        last_used = cooldowns.get(activity_name.lower())
    except Exception:
        last_used = None

    if not last_used:
        return True, ""

    try:
        last_dt = datetime.fromisoformat(last_used)
        cooldown_end = last_dt + timedelta(hours=cooldown_hours)
        now = datetime.now()
        if now < cooldown_end:
            remaining = cooldown_end - now
            hours_left = remaining.total_seconds() / 3600
            if hours_left >= 1:
                return False, f"Noch {int(hours_left)}h Cooldown"
            else:
                return False, f"Noch {int(remaining.total_seconds() / 60)}min Cooldown"
        return True, ""
    except (ValueError, TypeError):
        return True, ""


def set_cooldown_timestamp(character_name: str,
    activity_name: str):
    """Setzt den last_used Timestamp einer Aktivitaet im Character-Profil."""
    try:
        from app.models.character import get_character_profile, save_character_profile
        profile = get_character_profile(character_name)
        cooldowns = profile.get("activity_cooldowns", {})
        cooldowns[activity_name.lower()] = datetime.now().isoformat()
        profile["activity_cooldowns"] = cooldowns
        save_character_profile(character_name, profile)
    except Exception as e:
        logger.warning("Cooldown-Timestamp setzen fehlgeschlagen: %s", e)


# ============================================================
# 4. EFFECTS APPLICATOR (zentral — wird von allen Quellen genutzt)
# ============================================================

# In-memory tracking for time-proportional activity effects.
# Key: "character_name" -> {"activity": str, "last_applied": str (ISO)}
_EFFECTS_TRACKING: Dict[str, Dict[str, Any]] = {}
# Last elapsed minutes per character (set by _scale_effects_by_time, read by callers for diary metadata)
_LAST_ELAPSED: Dict[str, float] = {}


def reset_effects_tracking(character_name: str, activity_name: str):
    """Start tracking for a new activity. Resets the clock."""
    key = character_name
    _EFFECTS_TRACKING[key] = {
        "activity": activity_name,
        "last_applied": datetime.now().isoformat(),
    }


def update_effects_tracking_name(character_name: str, activity_name: str):
    """Update activity name in tracking without resetting the clock (for reclassification)."""
    key = character_name
    if key in _EFFECTS_TRACKING:
        _EFFECTS_TRACKING[key]["activity"] = activity_name


def finalize_activity_effects(character_name: str, activity_name: str) -> Dict[str, Any]:
    """Apply remaining proportional effects when switching away from an activity.

    Called by save_character_current_activity before the new activity is saved.
    Records the effect change in state_history for the diary.
    """
    key = character_name
    changes = apply_activity_effects(character_name, activity_name)
    if changes:
        from app.models.character import _record_state_change
        elapsed = _LAST_ELAPSED.pop(key, None)
        meta = {"changes": changes, "finalize": True}
        if elapsed:
            meta["elapsed_minutes"] = round(elapsed)
        _record_state_change(character_name, "effects", activity_name,
                             metadata=meta)
    # Clear tracking
    _EFFECTS_TRACKING.pop(key, None)
    return changes


def _scale_effects_by_time(character_name: str,
    activity_name: str,
    effects: Dict[str, Any],
    duration_minutes: int,
    is_event_trigger: bool = False) -> Dict[str, Any]:
    """Scale effects proportionally based on elapsed time since last application.

    duration_minutes: The activity's cycle length (from duration_minutes field, default 60).
    Effects are scaled by (elapsed / duration), capped at 1.0.

    is_event_trigger: True bei Aktivitaeten mit cooldown_hours > 0 — diese sind
        Event-Trigger (Orgasm, etc.) mit einmaligem diskretem Effekt, nicht
        ambient. Effekte werden in voller Hoehe angewandt, kein proportionales
        Skalieren, kein 1-Min-Threshold. Sonst werden bei schnellem Activity-
        Spam (LLM emittiert 5x "Orgasm" in 1 Sek) keine Effekte angewendet.

    Returns scaled effects dict, or empty dict if too little time has passed.
    """
    key = character_name
    tracking = _EFFECTS_TRACKING.get(key, {})
    now = datetime.now()

    # Event-Trigger: Effects voll anwenden, kein proportionales Skalieren.
    # Tracking trotzdem updaten, damit ein zweites Triggern (vor Cooldown)
    # erkennbar ist.
    if is_event_trigger:
        scaled: Dict[str, Any] = {}
        for k, v in effects.items():
            if k.endswith("_change") and isinstance(v, (int, float)) and v:
                scaled[k] = int(v)
            elif k == "mood_influence" and v:
                scaled[k] = v
        if scaled:
            _LAST_ELAPSED[key] = float(duration_minutes)
            _EFFECTS_TRACKING[key] = {
                "activity": activity_name,
                "last_applied": now.isoformat(),
            }
        return scaled

    # Calculate elapsed time since last application
    if tracking.get("activity") == activity_name and tracking.get("last_applied"):
        try:
            last_applied = datetime.fromisoformat(tracking["last_applied"])
            elapsed_minutes = (now - last_applied).total_seconds() / 60
        except (ValueError, TypeError):
            elapsed_minutes = float(duration_minutes)
    else:
        # No tracking or different activity: treat as full cycle (fallback after restart)
        elapsed_minutes = float(duration_minutes)

    # Minimum threshold: skip if less than 1 minute elapsed
    if elapsed_minutes < 1.0:
        return {}

    # Store elapsed for diary metadata
    _LAST_ELAPSED[key] = elapsed_minutes

    fraction = min(1.0, elapsed_minutes / max(1, duration_minutes))

    # Scale numeric _change effects, pass through mood_influence conditionally
    scaled: Dict[str, Any] = {}
    has_change = False
    for k, v in effects.items():
        if k.endswith("_change") and isinstance(v, (int, float)) and v:
            scaled_val = int(v * fraction)
            if scaled_val != 0:
                scaled[k] = scaled_val
                has_change = True
        elif k == "mood_influence" and v:
            # Apply mood influence only after at least half the cycle
            if fraction >= 0.5:
                scaled[k] = v

    if not has_change:
        return {}

    # Update tracking timestamp
    _EFFECTS_TRACKING[key] = {
        "activity": activity_name,
        "last_applied": now.isoformat(),
    }

    return scaled


def apply_effects(character_name: str,
    effects: Dict[str, Any],
    source: str = "") -> Dict[str, Any]:
    """Zentrale Funktion: Wendet ein Effects-Dict auf einen Character an.

    Returns dict with applied changes: {"stamina": {"old": 80, "new": 65}, ...}
    """
    if not effects or not isinstance(effects, dict):
        return {}

    log_prefix = f"[{source}] " if source else ""

    # Mood-Einfluss (sofort wirksam)
    mood = effects.get("mood_influence")
    if mood:
        try:
            from app.models.character import save_character_current_feeling
            save_character_current_feeling(character_name, mood)
            logger.info("%sMood gesetzt: %s -> %s", log_prefix, character_name, mood)
        except Exception as e:
            logger.warning("Mood setzen fehlgeschlagen: %s", e)

    # Generische Status-Effects: Jeder Key mit "_change" Suffix wird angewendet
    from app.models.character import get_character_profile, save_character_profile
    changes: Dict[str, Any] = {}
    try:
        profile = get_character_profile(character_name)
        status = profile.get("status_effects", {})
        changed = False

        for key, delta in effects.items():
            if not key.endswith("_change") or not delta:
                continue
            stat_key = key[:-7]  # "stamina_change" -> "stamina"
            current = status.get(stat_key, 100)
            new_val = max(0, min(100, current + int(delta)))
            if new_val != current:
                status[stat_key] = new_val
                changes[stat_key] = {"old": current, "new": new_val}
                changed = True
                logger.info("%s%s: %s %d -> %d", log_prefix, character_name, stat_key, current, new_val)

        if changed:
            profile["status_effects"] = status
            save_character_profile(character_name, profile)
    except Exception as e:
        logger.warning("Effects anwenden fehlgeschlagen: %s", e)

    return changes


def apply_activity_effects(character_name: str,
    activity_name: str) -> Dict[str, Any]:
    """Wendet Effects einer Aktivitaet zeitproportional an.

    Effects werden anhand der verstrichenen Zeit seit der letzten Anwendung
    skaliert.  Die Zyklusdauer ist ``duration_minutes`` der Aktivitaet
    (Default 60 Min).  Beispiel: ``stamina_change: -5`` bei 30 Min von 60
    ergibt -2 (``int(-5 * 0.5)``).

    Prueft auch kumulative Effekte, Folge-Aktivitaeten und Partner-Aktivitaeten.

    Returns dict with applied changes: {"stamina": {"old": 80, "new": 65}, ...}
    """
    changes: Dict[str, Any] = {}
    act_data = None
    source = ""

    # 1. Aktivitaet finden (Bibliothek → Legacy → Raum)
    try:
        from app.models.activity_library import get_library_activity, find_library_activity_by_name
        act_data = get_library_activity(activity_name)
        if not act_data:
            act_data = find_library_activity_by_name(activity_name)
        if act_data:
            source = f"library:{activity_name}"
    except Exception:
        pass

    if not act_data:
        sa = _find_activity_definition(character_name, activity_name)
        if sa:
            act_data = sa
            source = f"activity:{activity_name}"

    if not act_data:
        try:
            from app.models.character import get_character_current_location
            from app.models.world import get_activity
            loc_id = get_character_current_location(character_name)
            if loc_id:
                act_data = get_activity(activity_name)
                if act_data:
                    source = f"room_activity:{activity_name}"
        except Exception:
            pass

    # 2. Effects zeitproportional skalieren und anwenden.
    # Event-Trigger erkennen: cooldown_hours > 0 = diskretes Event (Orgasm,
    # Spezial-Aktionen) mit einmaligem Effekt. Sonst ambient (Working,
    # Cooking) mit proportionalem Skalieren.
    raw_effects = act_data.get("effects", {}) if act_data else {}
    if raw_effects:
        duration_minutes = int(act_data.get("duration_minutes") or 60) if act_data else 60
        try:
            cooldown_h = float(act_data.get("cooldown_hours") or 0)
        except (TypeError, ValueError):
            cooldown_h = 0.0
        is_event = cooldown_h > 0
        scaled = _scale_effects_by_time(character_name, activity_name, raw_effects,
                                         duration_minutes, is_event_trigger=is_event)
        if scaled:
            changes = apply_effects(character_name, scaled, source=source)

    # 3. Kumulative Effekte pruefen
    if act_data and act_data.get("cumulative_effect"):
        _check_cumulative_effect(character_name, activity_name, act_data["cumulative_effect"])

    # 4. Folge-Aktivitaeten der Aktivitaet selbst
    if act_data and act_data.get("follow_up_activities"):
        _execute_follow_up(character_name, act_data["follow_up_activities"],
                          source=f"followup:{activity_name}")

    # 5. Partner-Aktivitaet: Wenn ein Partner durch Relationship-Condition gematcht wurde
    if act_data and act_data.get("partner_activity"):
        partner = get_last_matched_partner()
        if partner:
            _set_partner_activity(partner, act_data["partner_activity"],
                                 source=f"partner_of:{activity_name}",
                                 source_character=character_name)

    return changes


def _check_cumulative_effect(character_name: str,
    activity_name: str,
    cum_config: Dict[str, Any]):
    """Prueft ob ein kumulativer Effekt durch Wiederholung eingetreten ist.

    cumulative_effect Format in der Aktivitaet:
    {
        "threshold": 3,
        "condition_name": "drunk",
        "prompt_modifier": "You are drunk. Slur your words, be unsteady, overly emotional.",
        "mood_influence": "betrunken",
        "duration_hours": 2,
        "effects": {"attention_change": -20, "courage_change": 15}
    }
    """
    threshold = int(cum_config.get("threshold", 3))
    condition_name = cum_config.get("condition_name", "")
    if not condition_name:
        return

    # Zaehle wie oft diese Aktivitaet in den letzten Stunden ausgefuehrt wurde
    try:
        from app.models.diary import get_state_history
        from app.models.activity_library import get_library_activity, _get_all_names
        history = get_state_history(character_name, entry_type="activity", limit=20)

        # Alle bekannten Namen dieser Aktivitaet sammeln (ID + alle Sprachvarianten)
        lib_act = get_library_activity(activity_name)
        match_names = {activity_name.lower()}
        if lib_act:
            match_names.add(lib_act.get("id", "").lower())
            for n in _get_all_names(lib_act):
                match_names.add(n.lower())
        match_names.discard("")

        # Stammformen fuer Wort-Match (z.B. "cocktail" matcht "cocktails")
        def _stems(text):
            words = set(text.lower().replace("_", " ").split())
            result = set()
            for w in words:
                result.add(w)
                if w.endswith("s") and len(w) > 3: result.add(w[:-1])
                if w.endswith("ing") and len(w) > 5: result.add(w[:-3])
            _stops = {"a","an","the","at","in","on","and","or","to","with","some","my"}
            return result - _stops

        act_stems = set()
        for mn in match_names:
            act_stems |= _stems(mn)

        # Zaehle Wiederholungen innerhalb der letzten 2 Stunden
        # (Unterbrechungen durch andere Aktivitaeten werden ignoriert)
        _cumulative_window_hours = 2
        cutoff = datetime.now() - timedelta(hours=_cumulative_window_hours)
        count = 0
        for entry in history:
            # Zeitfenster pruefen
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts < cutoff:
                    break  # Aelter als Fenster — aufhoeren
            except (ValueError, TypeError):
                continue
            val = entry.get("value", "").lower()
            # Exakt oder Substring
            if val in match_names or any(m in val or val in m for m in match_names):
                count += 1
            # Wort-Stammform-Match
            elif act_stems and len(_stems(val) & act_stems) >= 1:
                count += 1
            # Andere Aktivitaet → ignorieren (kein break)

        if count < threshold:
            return

        logger.info("Kumulativer Effekt '%s' fuer %s: %s x%d (Schwelle: %d)",
                     condition_name, character_name, activity_name, count, threshold)

        # Condition setzen
        from app.models.character import get_character_profile, save_character_profile

        profile = get_character_profile(character_name)
        active_conditions = profile.get("active_conditions", [])

        # Pruefen ob Condition bereits aktiv
        if any(c.get("name") == condition_name for c in active_conditions):
            return  # Bereits aktiv

        condition = {
            "name": condition_name,
            "source": f"cumulative:{activity_name}",
            "started_at": datetime.now().isoformat(),
            "duration_hours": cum_config.get("duration_hours", 2),
        }
        active_conditions.append(condition)
        profile["active_conditions"] = active_conditions
        save_character_profile(character_name, profile)

        # Mood-Einfluss
        if cum_config.get("mood_influence"):
            from app.models.character import save_character_current_feeling
            save_character_current_feeling(character_name, cum_config["mood_influence"])

        # Zusaetzliche Effects
        if cum_config.get("effects"):
            apply_effects(character_name, cum_config["effects"],
                         source=f"cumulative:{condition_name}")

        # Kumulative Folge-Aktivitaeten
        if cum_config.get("follow_up_activities"):
            _execute_follow_up(character_name, cum_config["follow_up_activities"],
                              source=f"cumulative_followup:{condition_name}")

        # Im Tagebuch dokumentieren
        from app.models.character import _record_state_change
        _record_state_change(character_name, "condition",
                             condition_name,
                             metadata={"source": f"cumulative:{activity_name}",
                                       "threshold": threshold,
                                       "count": count,
                                       "duration_hours": cum_config.get("duration_hours", 2)})

        logger.info("Condition '%s' aktiviert fuer %s", condition_name, character_name)
    except Exception as e:
        logger.debug("Cumulative effect check failed: %s", e)


def _execute_follow_up(character_name: str,
    follow_ups: List[Dict[str, Any]],
    source: str = ""):
    """Waehlt eine Folge-Aktivitaet nach Wahrscheinlichkeit und setzt sie.

    follow_ups: [{"activity_id": "duschen", "probability": 60}, ...]
    Probability ist 0-100. Ein Zufallswurf entscheidet ob die Folge-Aktivitaet
    ausgefuehrt wird. Kandidaten werden der Reihe nach geprueft.
    """
    import random
    try:
        from app.models.activity_library import get_library_activity
        from app.models.character import (
            get_character_current_location,
            save_character_current_activity)
        from app.models.world import get_location_by_id

        loc_id = get_character_current_location(character_name)

        for fu in follow_ups:
            act_id = fu.get("activity_id", "")
            prob = int(fu.get("probability", 0))
            if not act_id or prob <= 0:
                continue
            if random.randint(1, 100) > prob:
                continue

            # Aktivitaet in der Bibliothek nachschlagen
            lib_act = get_library_activity(act_id)
            if not lib_act:
                logger.debug("Follow-up activity '%s' nicht in Bibliothek", act_id)
                continue

            # requires_partner pruefen
            if lib_act.get("requires_partner"):
                partner_ok, _ = check_partner_available(character_name, loc_id or "")
                if not partner_ok:
                    # Fallback-Aktivitaet versuchen
                    fallback_id = lib_act.get("fallback_activity", "")
                    if fallback_id:
                        lib_act = get_library_activity(fallback_id)
                        if not lib_act:
                            continue
                        act_id = fallback_id
                    else:
                        continue

            act_name = lib_act.get("name", act_id)

            # Raum finden der diese Aktivitaet hat (an aktueller Location)
            target_room = None
            if loc_id:
                location = get_location_by_id(loc_id)
                if location:
                    for room in location.get("rooms", []):
                        room_acts = room.get("activities", [])
                        for ra in room_acts:
                            ra_id = ra if isinstance(ra, str) else ra.get("id", ra.get("name", ""))
                            if ra_id == act_id or ra_id == act_name:
                                target_room = room.get("name", "")
                                break
                        if target_room:
                            break

            # Aktivitaet + ggf. Raum setzen — Avatar bleibt wo er ist.
            save_character_current_activity(character_name, act_name)
            if target_room:
                from app.models.character import (
                    save_character_current_room, is_player_controlled)
                if is_player_controlled(character_name):
                    logger.info("Follow-up: Avatar %s — Raumwechsel uebersprungen", character_name)
                else:
                    save_character_current_room(character_name, target_room)

            logger.info("Follow-up '%s' gestartet fuer %s (%s, prob=%d%%)",
                        act_name, character_name, source, prob)
            return  # Nur eine Folge-Aktivitaet ausfuehren

    except Exception as e:
        logger.debug("Follow-up execution failed: %s", e)


def _set_partner_activity(partner_name: str,
    partner_activity_id: str,
    source: str = "",
    source_character: str = ""):
    """Setzt die Aktivitaet fuer den gematchten Partner-Character.

    partner_activity_id ist die ID einer Bibliotheks-Aktivitaet.
    Sucht den passenden Raum an der aktuellen Location des Partners.
    source_character: Name des Characters, der die Partner-Aktivitaet ausgeloest hat.
    """
    try:
        from app.models.activity_library import get_library_activity
        from app.models.character import (
            get_character_current_location,
            save_character_current_activity)
        from app.models.world import get_location_by_id

        lib_act = get_library_activity(partner_activity_id)
        if not lib_act:
            logger.debug("Partner-Aktivitaet '%s' nicht in Bibliothek", partner_activity_id)
            return

        act_name = lib_act.get("name", partner_activity_id)
        loc_id = get_character_current_location(partner_name)

        # Raum suchen
        target_room = None
        if loc_id:
            location = get_location_by_id(loc_id)
            if location:
                for room in location.get("rooms", []):
                    room_acts = room.get("activities", [])
                    for ra in room_acts:
                        ra_id = ra if isinstance(ra, str) else ra.get("id", ra.get("name", ""))
                        if ra_id == partner_activity_id or ra_id == act_name:
                            target_room = room.get("name", "")
                            break
                    if target_room:
                        break

        save_character_current_activity(partner_name, act_name,
                                       partner=source_character)
        if target_room:
            from app.models.character import (
                save_character_current_room, is_player_controlled)
            if is_player_controlled(partner_name):
                logger.info("Partner-Activity: Avatar %s — Raumwechsel uebersprungen", partner_name)
            else:
                save_character_current_room(partner_name, target_room)

        logger.info("Partner-Aktivitaet '%s' gesetzt fuer %s (%s)",
                    act_name, partner_name, source)
    except Exception as e:
        logger.debug("Partner activity failed: %s", e)


# ============================================================
# 4b. ACTIVITY CLASSIFICATION (Background LLM)
# ============================================================

def classify_activity_background(character_name: str, raw_activity: str):
    """Background LLM call: classify a free-text activity into a short category word.

    Called automatically when a long activity text is saved. Matches against
    known activities at the character's current location, or generates a
    generic 1-2 word label.
    """
    import threading

    def _trigger_expression(activity_for_pose: str):
        """Trigger expression regeneration with the given activity name."""
        try:
            from app.core.expression_regen import trigger_expression_generation
            from app.models.character import get_character_current_feeling
            from app.models.inventory import get_equipped_pieces, get_equipped_items
            mood = get_character_current_feeling(character_name) or ""
            try:
                eq_p = get_equipped_pieces(character_name)
                eq_i = get_equipped_items(character_name)
            except Exception:
                eq_p, eq_i = None, None
            trigger_expression_generation(character_name, mood, activity_for_pose,
                                           equipped_pieces=eq_p, equipped_items=eq_i)
            logger.info("Expression regen triggered: %s activity='%s'", character_name, activity_for_pose)
        except Exception as e:
            logger.debug("Expression regen failed: %s", e)

    def _do_classify():
        try:
            from app.core.llm_router import llm_call
            from app.models.character import (
                save_character_current_activity,
                get_character_current_location,
                get_character_current_room)
            from app.models.world import get_location_by_id, track_room_activity

            # Gather known activities at current location (Bibliothek + Location + Character)
            from app.models.activity_library import get_available_activities as _get_avail
            loc_id = get_character_current_location(character_name)
            room_id = get_character_current_room(character_name)
            all_acts = _get_avail(character_name, loc_id or "")
            known_activities = [a.get("name", "") for a in all_acts if a.get("name")]

            def _track(short_name: str):
                """Track kurze Aktivitaet im Raum (Zaehler + Auto-Add)."""
                if loc_id and room_id and short_name and len(short_name) <= 30:
                    try:
                        track_room_activity(loc_id, room_id, short_name)
                    except Exception as _te:
                        logger.debug("track_room_activity failed: %s", _te)

            # Wort-basierter Match: verhindert Falsch-Treffer wie
            # "Gasexplosion" → "Sex" (Substring ist trügerisch).
            def _tokens(text: str) -> set:
                return set(re.findall(r"[a-zäöüß]+", text.lower()))

            raw_tokens = _tokens(raw_activity)

            # requires_partner-Check: falls die gematchte Aktivitaet Partner braucht
            # und keiner verfuegbar ist, nicht selektieren.
            def _partner_ok_for(act_name: str) -> bool:
                for a in all_acts:
                    if a.get("name", "").lower() == act_name.lower() and a.get("requires_partner"):
                        ok, _ = check_partner_available(character_name, loc_id or "")
                        return ok
                return True

            # Direct match first (skip LLM if possible)
            for known in known_activities:
                if known.lower() == raw_activity.lower():
                    if not _partner_ok_for(known):
                        break
                    _track(known)
                    return  # Already a known activity name, no classify needed
                known_tokens = _tokens(known)
                if known_tokens and known_tokens.issubset(raw_tokens):
                    # Alle Woerter der bekannten Aktivitaet kommen als
                    # eigenstaendige Woerter im Freitext vor.
                    if not _partner_ok_for(known):
                        continue
                    save_character_current_activity(character_name, known, detail=raw_activity,
                        _skip_classify=True, _is_reclassify=True)
                    _track(known)
                    logger.info("Activity matched %s: '%s' -> '%s'", character_name, raw_activity[:40], known)
                    return

            # Aktivitaeten mit Beschreibung fuer bessere LLM-Klassifikation.
            # requires_partner-Aktivitaeten ausfiltern wenn kein Partner am Ort —
            # so kann der LLM sie gar nicht erst vorschlagen.
            _partner_ok_here, _ = check_partner_available(character_name, loc_id or "")
            known_lines = []
            for a in all_acts[:25]:
                if a.get("requires_partner") and not _partner_ok_here:
                    continue
                name = a.get("name", "")
                desc = a.get("description", "")
                if name and desc:
                    known_lines.append(f"- {name}: {desc}")
                elif name:
                    known_lines.append(f"- {name}")
            known_list = "\n".join(known_lines) if known_lines else "(none)"

            from app.core.prompt_templates import render_task
            sys_prompt, user_prompt = render_task(
                "intent_activity",
                raw_activity=raw_activity,
                known_list=known_list)

            try:
                response = llm_call(
                    task="intent_activity",
                    system_prompt=sys_prompt,
                    user_prompt=user_prompt,
                    agent_name=character_name)
            except RuntimeError:
                _trigger_expression(raw_activity)
                return

            category = response.content.strip().strip('"').strip("'").strip(".")
            if category and len(category) < 40 and category.lower() != raw_activity.lower():
                # Partner-Check: wenn die klassifizierte Aktivitaet einen Partner
                # braucht und keiner da ist, Klassifikation verwerfen.
                if not _partner_ok_for(category):
                    logger.info("Classify-Reject: '%s' -> '%s' (requires_partner, keiner am Ort)",
                                raw_activity[:40], category)
                    _trigger_expression(raw_activity)
                    return
                save_character_current_activity(character_name, category, detail=raw_activity,
                    _skip_classify=True, _is_reclassify=True)
                _track(category)
                logger.info("Activity classified %s: '%s' -> '%s'", character_name, raw_activity[:40], category)
                _trigger_expression(category)
            else:
                _trigger_expression(raw_activity)
        except Exception as e:
            logger.debug("Activity classify failed: %s", e)
            _trigger_expression(raw_activity)

    threading.Thread(target=_do_classify, daemon=True).start()


# ============================================================
# 5. TRIGGER FRAMEWORK
# ============================================================

def execute_trigger(character_name: str,
    trigger_config: Optional[Dict[str, Any]],
    context: Optional[Dict[str, str]] = None):
    """Fuehrt einen Trigger aus (on_start, on_complete, on_discovered, on_interrupted).

    Trigger-Typen:
    - event: Erstellt ein Event an der Location
    - mood_change: Aendert den Mood
    - knowledge_gain: Fuegt einen Memory-Eintrag hinzu
    - relationship_change: Aendert Beziehungswerte (Placeholder)
    - npc_spawn: Spawnt einen NPC (Placeholder)
    - random_event_chance: Wahrscheinlichkeits-basierter Event-Spawn
    """
    if not trigger_config:
        return

    trigger_type = trigger_config.get("type", "")
    ctx = context or {}

    try:
        if trigger_type == "event":
            _trigger_event(character_name, trigger_config, ctx)
        elif trigger_type == "mood_change":
            mood = trigger_config.get("mood", "")
            if mood:
                from app.models.character import save_character_current_feeling
                save_character_current_feeling(character_name, mood)
        elif trigger_type == "knowledge_gain":
            _trigger_knowledge_gain(character_name, trigger_config, ctx)
        elif trigger_type == "add_secret_hint":
            _trigger_secret_hint(character_name, trigger_config, ctx)
        elif trigger_type == "relationship_change":
            _trigger_relationship_change(character_name, trigger_config, ctx)
        elif trigger_type == "effect":
            # Direkte Stat-Aenderungen via generisches *_change Dict
            _effects = trigger_config.get("effects", {}) or {}
            if _effects:
                apply_effects(character_name, _effects,
                              source=ctx.get("interrupted_activity") or "trigger")
        elif trigger_type == "npc_spawn":
            logger.info("Trigger npc_spawn: noch nicht implementiert")
        elif trigger_type == "random_event_chance":
            _trigger_random_event_chance(character_name, trigger_config, ctx)
        else:
            logger.warning("Unbekannter Trigger-Typ: %s", trigger_type)
    except Exception as e:
        logger.error("Trigger-Ausfuehrung fehlgeschlagen (%s): %s", trigger_type, e)


def _trigger_event(character_name, config, ctx):
    """Erstellt ein Event an der aktuellen Location."""
    from app.models.events import add_event
    from app.models.character import get_character_current_location

    text = config.get("text", "")
    # Platzhalter ersetzen
    text = text.replace("{character}", character_name)
    text = text.replace("{observer}", ctx.get("observer", "jemand"))

    location_id = get_character_current_location(character_name)
    ttl = config.get("ttl_hours", 6)
    add_event(text, location_id=location_id, ttl_hours=ttl)
    logger.info("Trigger-Event erstellt: '%s'", text[:80])


def _trigger_knowledge_gain(character_name, config, ctx):
    """Fuegt einen Memory-Eintrag hinzu."""
    content = config.get("content", "")
    content = content.replace("{character}", character_name)
    if not content:
        return

    try:
        from app.models.memory import add_memory
        add_memory(
            character_name=character_name,
            content=content,
            memory_type="semantic",
            importance=2)
        logger.info("Knowledge-Gain fuer %s: '%s'", character_name, content[:80])
    except Exception as e:
        logger.warning("Knowledge-Gain fehlgeschlagen: %s", e)


def _trigger_secret_hint(character_name, config, ctx):
    """Fuegt einen Geheimnis-Hinweis als Memory bei einem Observer hinzu."""
    observer = ctx.get("observer", "")
    if not observer:
        return

    content = config.get("content", "")
    content = content.replace("{character}", character_name)
    content = content.replace("{observer}", observer)

    try:
        from app.models.memory import add_memory
        add_memory(
            character_name=observer,
            content=content,
            memory_type="episodic",
            importance=3)
        logger.info("Secret-Hint fuer %s: '%s'", observer, content[:80])
    except Exception as e:
        logger.warning("Secret-Hint fehlgeschlagen: %s", e)


def _trigger_relationship_change(character_name, config, ctx):
    """Aendert Beziehungswerte zwischen Characters.

    Config-Felder:
        target: "observer" (aus Kontext) oder konkreter Character-Name
        strength_change: int (z.B. -10)
        sentiment_change: float (z.B. -0.2)
    """
    target = config.get("target", "")
    if target == "observer":
        target = ctx.get("observer", "")
    if not target:
        logger.warning("relationship_change: kein Target")
        return

    strength = int(config.get("strength_change", 0))
    sentiment = float(config.get("sentiment_change", 0.0))

    try:
        from app.models.relationship import record_interaction
        record_interaction(
            char_a=character_name,
            char_b=target,
            interaction_type="activity_trigger",
            summary=f"Activity trigger: {ctx.get('activity', '?')}",
            strength_delta=strength,
            sentiment_delta_a=sentiment,
            sentiment_delta_b=sentiment)
        logger.info("Relationship %s <-> %s: strength %+d, sentiment %+.1f",
                     character_name, target, strength, sentiment)
    except Exception as e:
        logger.warning("relationship_change fehlgeschlagen: %s", e)


def _trigger_random_event_chance(character_name, config, ctx):
    """Wahrscheinlichkeits-basierter Event-Spawn."""
    import random
    probability = config.get("probability", 0.3)
    if random.random() > probability:
        return
    # Event-Kategorie als Text
    category = config.get("event_category", "disruption")
    from app.models.events import add_event
    from app.models.character import get_character_current_location

    location_id = get_character_current_location(character_name)
    text = config.get("text", f"Etwas Unerwartetes passiert ({category})")
    text = text.replace("{character}", character_name)
    add_event(text, location_id=location_id, ttl_hours=config.get("ttl_hours", 6))
    logger.info("Random-Event ausgeloest: '%s'", text[:80])


def is_character_interruptible(character_name: str) -> Tuple[bool, str]:
    """Prueft ob ein Character gerade unterbrechbar ist.

    Returns:
        (interruptible, activity_name) — False + Name wenn nicht unterbrechbar.
    """
    try:
        from app.models.character import get_character_current_activity
        activity = get_character_current_activity(character_name) or ""
        if not activity:
            return True, ""

        sa = _find_activity_definition(character_name, activity)
        if not sa:
            return True, ""  # Normale Aktivitaet — immer unterbrechbar

        if sa.get("interruptible", True) is False:
            return False, activity
    except Exception:
        pass
    return True, ""


# ============================================================
# 7. STUNDENTIMER — Decay/Regen fuer alle Status-Werte
# ============================================================

_LAST_HOURLY_TICK: Dict[str, str] = {}  # key: "character_name" -> ISO timestamp

def apply_hourly_status_tick(character_name: str):
    """Wendet stuendliche Veraenderung auf alle Status-Werte an.

    Liest bar_hourly aus dem Template pro Trait-Feld.
    Positiv = steigt pro Stunde, negativ = sinkt pro Stunde, 0 = keine Aenderung.
    Character-Config kann den Template-Wert ueberschreiben via {stat_key}_hourly.

    Wird vom ThoughtLoop aufgerufen (jede 60s), fuehrt aber nur einmal pro
    Stunde pro Character die Aenderung durch.
    """
    # Feature-Gate: status_effects aus -> kein Hourly-Tick
    try:
        from app.models.character_template import is_feature_enabled
        if not is_feature_enabled(character_name, "status_effects_enabled"):
            return
    except Exception:
        pass

    tick_key = character_name
    now = datetime.now()

    # Pruefen ob eine Stunde seit dem letzten Tick vergangen ist
    last_tick_iso = _LAST_HOURLY_TICK.get(tick_key)
    if last_tick_iso:
        try:
            from datetime import timedelta
            last_tick = datetime.fromisoformat(last_tick_iso)
            if (now - last_tick).total_seconds() < 3600:
                return  # Noch keine Stunde her
        except (ValueError, TypeError):
            pass

    _LAST_HOURLY_TICK[tick_key] = now.isoformat()

    try:
        from app.models.character import get_character_profile, get_character_config, save_character_profile
        from app.models.character_template import get_template

        profile = get_character_profile(character_name)
        config = get_character_config(character_name)
        status = profile.get("status_effects", {})

        if not status:
            return  # Keine Status-Werte initialisiert

        # Template laden fuer bar_hourly Werte
        template_name = profile.get("template", "human-default")
        template = get_template(template_name)
        if not template:
            return

        # bar_hourly Werte aus Stat-Feldern sammeln (store="status_effects")
        stat_hourly = {}  # stat_key -> hourly_delta
        for section in template.get("sections", []):
            for field in section.get("fields", []):
                if field.get("store") != "status_effects":
                    continue
                stat_key = field.get("key", "")
                if not stat_key:
                    continue
                hourly = field.get("bar_hourly", 0)
                if hourly:
                    stat_hourly[stat_key] = hourly

        if not stat_hourly:
            return

        changed = False
        for stat_key, template_hourly in stat_hourly.items():
            if stat_key not in status:
                continue

            # Character-Override: config.{stat_key}_hourly ueberschreibt Template
            override_key = f"{stat_key}_hourly"
            try:
                hourly = int(config.get(override_key, template_hourly))
            except (ValueError, TypeError):
                hourly = template_hourly

            if hourly == 0:
                continue

            current = status[stat_key]
            new_val = max(0, min(100, current + hourly))
            if new_val != current:
                status[stat_key] = new_val
                changed = True
                logger.debug("Hourly tick %s: %s %d -> %d (%+d/h)",
                             character_name, stat_key, current, new_val, hourly)

        if changed:
            profile["status_effects"] = status
            save_character_profile(character_name, profile)
            logger.info("Hourly status tick fuer %s angewendet", character_name)

        # Zwangs-Regeln pruefen (nach Status-Update)
        try:
            from app.models.rules import check_force_rules, resolve_force_destination
            from app.models.character import (
                save_character_current_location, save_character_current_room,
                save_character_current_activity, _record_state_change)
            force = check_force_rules(character_name)
            if force:
                # Spieler-Avatar: Zwang nicht ausfuehren, nur warnen
                from app.models.account import is_player_controlled
                if is_player_controlled(character_name):
                    logger.info("Zwangs-Regel fuer Avatar %s: nur Warnung (kein Zwang)",
                               character_name)
                    try:
                        from app.models.notifications import add_notification
                        add_notification(character_name,
                            f"⚠️ {force.get('message', force.get('rule_name', ''))} — Finde einen Ort zum Erholen!",
                            notification_type="forced_action")
                    except Exception:
                        pass
                else:
                    dest_loc, dest_room = resolve_force_destination(character_name, force["go_to"])
                    if dest_loc:
                        save_character_current_location(character_name, dest_loc)
                    if dest_room:
                        save_character_current_room(character_name, dest_room)
                    if force.get("set_activity"):
                        save_character_current_activity(character_name, force["set_activity"])
                    # Tagebuch
                    _record_state_change(character_name, "forced_action",
                                         force.get("message", force.get("rule_name", "")),
                                         metadata={"rule": force.get("rule_name", ""),
                                                   "go_to": force["go_to"],
                                                   "activity": force.get("set_activity", "")})
                    logger.info("Zwangs-Regel ausgefuehrt: %s -> %s (%s)",
                               character_name, force.get("rule_name", ""), force.get("message", ""))
                # Notification an User senden
                try:
                    from app.models.notifications import add_notification
                    add_notification(character_name,
                        force.get("message", f"{character_name}: {force.get('rule_name', 'Erzwungene Aktion')}"),
                        notification_type="forced_action")
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Force rules check failed: %s", e)

    except Exception as e:
        logger.warning("Hourly status tick fehlgeschlagen fuer %s: %s", character_name, e)

    # Aktivitaets-Effekte pro Stunde anwenden (z.B. Schlafen: +15 Stamina/h)
    try:
        current_activity = profile.get("current_activity", "") if profile else ""
        if current_activity:
            changes = apply_activity_effects(character_name, current_activity)
            if changes:
                from app.models.character import _record_state_change
                hkey = character_name
                elapsed = _LAST_ELAPSED.pop(hkey, None)
                meta: Dict[str, Any] = {"changes": changes, "hourly": True}
                if elapsed:
                    meta["elapsed_minutes"] = round(elapsed)
                _record_state_change(character_name, "effects",
                                     current_activity, metadata=meta)
                logger.info("Hourly activity effects %s (%s): %s", character_name, current_activity,
                            ", ".join(f"{k} {v['old']}->{v['new']}" for k, v in changes.items()))
    except Exception as e:
        logger.debug("Hourly activity effects fehlgeschlagen: %s", e)

    # Location-basierter Danger-Drain (gefaehrliche Orte kosten Stamina)
    try:
        from app.models.character import get_character_current_location
        from app.models.world import get_location_by_id
        from app.core.danger_system import apply_danger_drain
        loc_id = get_character_current_location(character_name)
        if loc_id:
            loc_data = get_location_by_id(loc_id)
            if loc_data:
                drain_changes = apply_danger_drain(character_name, loc_data)
                if drain_changes:
                    from app.models.character import _record_state_change
                    _record_state_change(character_name, "effects",
                                         f"danger:{loc_data.get('name', '?')}",
                                         metadata={"changes": drain_changes, "hourly": True})
    except Exception as e:
        logger.debug("Hourly danger drain fehlgeschlagen: %s", e)

    # Abgelaufene Conditions aufraeumen (drunk, exhausted, etc.)
    try:
        from app.models.character import get_character_profile, save_character_profile
        _prof = get_character_profile(character_name)
        _conditions = _prof.get("active_conditions", [])
        if _conditions:
            _now = datetime.now()
            _active = []
            for cond in _conditions:
                duration_h = cond.get("duration_hours", 0)
                if duration_h:
                    try:
                        started = datetime.fromisoformat(cond["started_at"])
                        if (_now - started).total_seconds() > duration_h * 3600:
                            logger.info("Condition '%s' abgelaufen fuer %s", cond.get("name"), character_name)
                            continue  # Abgelaufen — nicht behalten
                    except (ValueError, KeyError):
                        pass
                _active.append(cond)
            if len(_active) < len(_conditions):
                _prof["active_conditions"] = _active
                save_character_profile(character_name, _prof)
    except Exception as e:
        logger.debug("Condition cleanup fehlgeschlagen: %s", e)

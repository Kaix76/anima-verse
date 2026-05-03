"""Danger System — Location-Gefahren, Zugangs-Checks, Stamina-Drain.

Locations koennen folgende Felder haben:
- danger_level (0-5): Allgemeines Gefahrenniveau
- restrictions: Zugangsbeschraenkungen
- hazards: Moegliche Gefahren-Events

Zustandswerte (stamina, courage, stress) existieren bereits in
character_profile["status_effects"] und werden vom activity_engine verwaltet.
"""
from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("danger_system")


# ============================================================
# 1. LOCATION DANGER HELPERS
# ============================================================

def get_danger_level(location: Dict[str, Any]) -> int:
    """Gibt danger_level einer Location zurueck (0-5, default 0)."""
    try:
        return max(0, min(5, int(location.get("danger_level", 0))))
    except (ValueError, TypeError):
        return 0


def get_restrictions(location: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy — gibt leeres Dict zurueck. Restrictions durch Rules Engine ersetzt."""
    return {}


def get_hazards(location: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gibt hazards-Liste einer Location zurueck."""
    return location.get("hazards") or []


# ============================================================
# 2. ZUGANGS-CHECK (fuer SetLocationSkill)
# ============================================================

def check_location_access(character_name: str,
    location: Dict[str, Any]) -> Tuple[bool, str]:
    """Prueft ob ein Character eine Location betreten darf.

    Delegiert an die Rules Engine (app/models/rules.py).
    Alte restrictions wurden entfernt — alles laeuft ueber Rules.
    """
    from app.models.rules import check_access
    loc_id = location.get("id", "")
    return check_access(character_name, loc_id)


# ============================================================
# 3. STAMINA-DRAIN (pro Stunde, basierend auf danger_level)
# ============================================================

def apply_danger_drain(character_name: str, location: Dict[str, Any]) -> Dict[str, Any]:
    """Wendet stuendliche Stat-Drains basierend auf danger_level an.

    Generisch: welche Stats wie stark gedraint werden kommt aus den
    Location-Restrictions oder der globalen Welt-Config — NICHT hartcodiert.

    Restrictions-Format:
        restrictions.stat_drains: {"stamina": -3, "courage": -1}   # explizit
        restrictions.drain_level_scale: {"stamina": 1.0, ...}      # optional Skalierung

    Fallback pro danger_level (wenn stat_drains fehlt):
        Skalierung via `EVENT_DANGER_DRAINS` JSON-Env oder leer.
        Leer = kein Drain (nutzt dann Welt-Regeln / Activities).

    Returns:
        Dict mit Aenderungen {"stat": {"old": X, "new": Y}} oder leer.
    """
    danger = get_danger_level(location)
    restrictions = get_restrictions(location)

    # 1. Explizite stat_drains in Location-Restrictions hat Vorrang
    stat_drains = restrictions.get("stat_drains")
    if not stat_drains:
        # 2. Globale Default-Skalierung pro danger_level aus Env/Config
        # Format: {"2": {"stamina": -3}, "3": {"stamina": -6, "courage": -1}, ...}
        import os, json as _json
        try:
            defaults_raw = os.environ.get("DANGER_STAT_DRAINS", "")
            if defaults_raw:
                defaults = _json.loads(defaults_raw)
                stat_drains = defaults.get(str(danger)) or defaults.get(danger, {})
        except Exception:
            stat_drains = None

    if not stat_drains:
        return {}

    # In effects-Format umwandeln ({stat_change: delta})
    effects = {f"{stat}_change": delta for stat, delta in stat_drains.items()
               if isinstance(delta, (int, float)) and delta != 0}
    if not effects:
        return {}

    try:
        from app.core.activity_engine import apply_effects
        return apply_effects(character_name,
            effects,
            source=f"danger:{location.get('name', '?')}")
    except Exception as e:
        logger.warning("Danger drain fehlgeschlagen: %s", e)
        return {}


# ============================================================
# 4. PROMPT-MODIFIER (Status-Effekte im System-Prompt)
# ============================================================

def _load_status_modifiers() -> List[Dict[str, Any]]:
    """Laedt konfigurierbare Status-Modifier aus der Config.

    Keine hartcodierten Defaults — wenn die Welt keine Modifier-Datei hat,
    werden auch keine angewandt. Modifier muss der User im Game Admin anlegen.
    """
    try:
        import json as _json
        path = get_storage_dir() / "status_modifiers.json"
        if path.exists():
            data = _json.loads(path.read_text(encoding="utf-8"))
            return data.get("modifiers", [])
    except Exception:
        pass
    return []

def save_status_modifiers(modifiers: List[Dict[str, Any]]):
    """Speichert Status-Modifier in die Config."""
    import json as _json
    path = get_storage_dir() / "status_modifiers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({"modifiers": modifiers}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def build_status_prompt_section(character_name: str) -> str:
    """DEACTIVATED — replaced by ``app.core.prompt_filters``.

    The rules-table-driven effects/modifier/danger rendering moved to a
    state-filter mechanism that not only adds prompt text but also drops
    irrelevant blocks (e.g. fuzzy memory while drunk). This function now
    returns "" so existing callers (currently
    ``thought_context._build_effects_block``) become no-ops; the filter
    layer overrides ``ctx['effects_block']`` after building.
    """
    return ""

    # ----- legacy implementation kept below for reference / future re-enable -----
    try:
        from app.models.character import get_character_profile, get_character_current_location, get_character_current_room
        from app.models.world import get_location_by_id
        from app.core.activity_engine import evaluate_condition

        profile = get_character_profile(character_name)
        status = profile.get("status_effects", {})
        loc_id = get_character_current_location(character_name) or ""

        lines = []

        # Konfigurierbare Status-Modifier (erste matchende pro Stat-Gruppe).
        # condition:X wird hier NICHT behandelt — kommt weiter unten als active_conditions.
        matched_stats = set()
        for mod in _load_status_modifiers():
            condition = mod.get("condition", "")
            modifier = mod.get("prompt_modifier", "")
            if not condition or not modifier:
                continue
            if condition.startswith("condition:"):
                continue
            # Stat-Gruppe ermitteln (z.B. "stamina" aus "stamina<30")
            stat_group = condition.split("<")[0].split(">")[0].split("=")[0].strip()
            if stat_group in matched_stats:
                continue  # Nur der erste Match pro Stat (strengste Bedingung zuerst)
            passed, _ = evaluate_condition(condition, character_name, loc_id)
            if passed:
                matched_stats.add(stat_group)
                lines.append(f"- {modifier}")

        # Danger-Level (Location oder Raum)
        if loc_id:
            loc_data = get_location_by_id(loc_id)
            if loc_data:
                # Raum-Danger ueberschreibt Location-Danger
                room_id = get_character_current_room(character_name) or ""
                danger = get_danger_level(loc_data)
                if room_id:
                    for room in loc_data.get("rooms", []):
                        if room.get("id") == room_id and room.get("danger_level") is not None:
                            danger = max(0, min(5, int(room["danger_level"])))
                            break
                if danger >= 3:
                    loc_name = loc_data.get("name", "?")
                    lines.append(f"- You are at a dangerous location: {loc_name} (danger level {danger}/5). Stay alert.")

        # Aktive Conditions (kumulative Effekte: drunk, exhausted, etc.)
        # Werden staerker betont als normale Status-Modifier, damit das LLM
        # sie nicht im Rest des Prompts uebergeht.
        active_conditions = profile.get("active_conditions", [])
        condition_lines: List[str] = []
        condition_names: List[str] = []
        if active_conditions:
            now = datetime.now()
            modifiers_config = _load_status_modifiers()
            modifier_by_condition = {}
            for mod in modifiers_config:
                cond_str = mod.get("condition", "")
                if cond_str.startswith("condition:"):
                    cond_name = cond_str[10:].strip()
                    modifier_by_condition[cond_name.lower()] = mod.get("prompt_modifier", "")

            for cond in active_conditions:
                duration_h = cond.get("duration_hours", 0)
                if duration_h:
                    try:
                        started = datetime.fromisoformat(cond["started_at"])
                        if (now - started).total_seconds() > duration_h * 3600:
                            continue  # Abgelaufen
                    except (ValueError, KeyError):
                        pass
                cond_name = cond.get("name", "")
                if not cond_name:
                    continue
                condition_names.append(cond_name)
                modifier = (modifier_by_condition.get(cond_name.lower(), "")
                            or f"You are currently {cond_name}.")
                condition_lines.append(f"- {modifier}")

        if not lines and not condition_lines:
            return ""

        sections: List[str] = []
        if condition_lines:
            names_str = ", ".join(condition_names).upper()
            sections.append(
                f"\nCRITICAL STATE — YOU ARE {names_str}. This overrides your normal behavior. "
                f"Every reply MUST reflect this state in tone, wording, and actions. Do not act "
                f"as if you were sober/normal. Show it clearly in your next response:\n"
                + "\n".join(condition_lines)
            )
        if lines:
            sections.append("\nYour current physical/mental state:\n" + "\n".join(lines))

        return "\n".join(sections)

    except Exception as e:
        logger.debug("build_status_prompt_section failed: %s", e)
        return ""


def build_condition_reminder(character_name: str) -> str:
    """Kurzer Reminder fuer aktive Conditions — fuer Ende des System-Prompts.

    Wird spaet im Prompt platziert, damit das LLM es waehrend der Generierung
    staerker beruecksichtigt als die frueh platzierte Status-Sektion.
    """
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) or {}
        active = profile.get("active_conditions", []) or []
        if not active:
            return ""

        now = datetime.now()
        names: List[str] = []
        for cond in active:
            duration_h = cond.get("duration_hours", 0) or 0
            if duration_h:
                try:
                    started = datetime.fromisoformat(cond["started_at"])
                    if (now - started).total_seconds() > duration_h * 3600:
                        continue
                except (ValueError, KeyError):
                    pass
            name = (cond.get("name") or "").strip()
            if name:
                names.append(name)
        if not names:
            return ""
        names_str = ", ".join(names).upper()
        return (f"\nREMINDER — YOU ARE {names_str}. Your next response MUST clearly show this "
                f"state. Do not default to normal/sober behavior.")
    except Exception:
        return ""


def get_active_condition_image_modifiers(character_name: str) -> str:
    """Sammelt image_modifier aller aktiven (nicht abgelaufenen) Conditions.

    Wird an den Expression-Prompt angehaengt, damit die Bildgenerierung
    die visuellen Hinweise (z.B. 'flushed cheeks, glassy eyes' bei drunk)
    beruecksichtigt.
    """
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) or {}
        active = profile.get("active_conditions", []) or []
        if not active:
            return ""

        # Index: condition_name -> image_modifier
        image_by_condition: Dict[str, str] = {}
        for mod in _load_status_modifiers():
            cond_str = mod.get("condition", "") or ""
            if cond_str.startswith("condition:"):
                name = cond_str[10:].strip().lower()
                img_mod = (mod.get("image_modifier", "") or "").strip()
                if img_mod:
                    image_by_condition[name] = img_mod

        now = datetime.now()
        parts: List[str] = []
        for cond in active:
            duration_h = cond.get("duration_hours", 0) or 0
            if duration_h:
                try:
                    started = datetime.fromisoformat(cond["started_at"])
                    if (now - started).total_seconds() > duration_h * 3600:
                        continue
                except (ValueError, KeyError):
                    pass
            name = (cond.get("name") or "").strip().lower()
            img_mod = image_by_condition.get(name, "")
            if img_mod:
                parts.append(img_mod)
        return ", ".join(parts)
    except Exception as e:
        logger.debug("get_active_condition_image_modifiers failed: %s", e)
        return ""



"""Outfit-Consistency-Rules-Engine.

Liefert zu einem (outfit_type, character, activity) die tatsaechlich erforderlichen
Slots — Baseline + Character-Exceptions + Runtime-Force-Skip + Activity-Force.

Phase 1: reine Berechnung, keine Side-Effects.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.core.log import get_logger
from app.core.paths import get_config_dir

logger = get_logger("outfit_rules")


def _rules_path() -> Path:
    return get_config_dir() / "outfit_rules.json"


@lru_cache(maxsize=1)
def _load_rules() -> Dict:
    path = _rules_path()
    if not path.exists():
        logger.warning("outfit_rules.json fehlt: %s", path)
        return {"outfit_types": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("outfit_rules.json nicht lesbar: %s", e)
        return {"outfit_types": {}}


def reload_rules() -> None:
    _load_rules.cache_clear()


def baseline_required_slots(outfit_type: str) -> List[str]:
    """Baseline required slots fuer einen outfit_type — ohne Character-Exceptions.

    Case-insensitive Lookup — "Sport", "sport", "SPORT" liefern dasselbe.
    """
    rules = _load_rules()
    key = (outfit_type or "").strip().lower()
    for k, v in rules.get("outfit_types", {}).items():
        if k.lower() == key:
            return list(v.get("required", []))
    return []


def known_outfit_types() -> List[str]:
    return list(_load_rules().get("outfit_types", {}).keys())


def default_outfit_type() -> str:
    """Liefert den als ``default: true`` markierten outfit_type aus den
    Regeln, oder ''. Wird als Fallback genutzt wenn weder Activity, Raum
    noch Location einen Type vorgeben.
    """
    rules = _load_rules()
    for name, entry in (rules.get("outfit_types") or {}).items():
        if isinstance(entry, dict) and entry.get("default"):
            return name
    return ""


def resolve_target_outfit_type(character_name: str) -> str:
    """Zentrale Aufloesung des aktuell relevanten outfit_type.

    Prioritaet (hoch → niedrig):
        1. Activity (aus Library: activity.outfit_type)
        2. Raum (room.outfit_type)
        3. Location (location.outfit_type)
        4. Default-Regel (outfit_rules.json Eintrag mit ``default: true``)
        5. '' (kein Type → Compliance laeuft nicht)

    Activity gewinnt, weil die Taetigkeit die semantisch genaueste Aussage
    ueber passende Kleidung ist ("Sonnenbaden" → swimwear, auch wenn der
    Raum generisch "Outdoor" ist).
    """
    if not character_name:
        return ""
    try:
        from app.models.character import (
            get_character_profile, get_character_current_activity,
            get_character_current_location, get_character_current_room)
    except Exception:
        return ""

    # 1) Activity
    try:
        activity = (get_character_current_activity(character_name) or "").strip()
        if activity:
            from app.models.activity_library import get_library_activity, find_library_activity_by_name
            act = get_library_activity(activity) or find_library_activity_by_name(activity)
            if act:
                t = (act.get("outfit_type") or "").strip()
                if t:
                    return t
    except Exception as e:
        logger.debug("Activity-outfit_type lookup fehlgeschlagen: %s", e)

    # 2) Raum
    try:
        loc_id = get_character_current_location(character_name) or ""
        room_id = get_character_current_room(character_name) or ""
        if loc_id:
            from app.models.world import get_location_by_id
            loc = get_location_by_id(loc_id) or {}
            if room_id:
                for r in (loc.get("rooms") or []):
                    if r.get("id") == room_id or r.get("name") == room_id:
                        t = (r.get("outfit_type") or "").strip()
                        if t:
                            return t
                        break
            # 3) Location
            t = (loc.get("outfit_type") or "").strip()
            if t:
                return t
    except Exception as e:
        logger.debug("Location/Room-outfit_type lookup fehlgeschlagen: %s", e)
    # 4) Default aus den Regeln
    return default_outfit_type()


def resolve_required_slots(
    outfit_type: str,
    character_name: str = "",
    activity_force_required: Optional[List[str]] = None,
    activity_force_skip: Optional[List[str]] = None,
    runtime_force_skip: Optional[List[str]] = None,
) -> Set[str]:
    """Liefert die effektive Menge erforderlicher Slots nach allen Regeln.

    Prioritaet (hoch → niedrig):
    1. runtime_force_skip (Chat-Extractor: "zieht sich aus")
    2. activity_force_skip / activity_force_required
    3. character outfit_exceptions.skip_required
    4. outfit_type-Baseline
    """
    required: Set[str] = set(baseline_required_slots(outfit_type))

    # Character-Exceptions — case-insensitive Lookup analog baseline_required_slots
    if character_name:
        try:
            from app.models.character import get_character_profile
            profile = get_character_profile(character_name) or {}
            exceptions = profile.get("outfit_exceptions") or {}
            key = (outfit_type or "").strip().lower()
            exc = {}
            for k, v in exceptions.items():
                if isinstance(k, str) and k.strip().lower() == key:
                    exc = v or {}
                    break
            for slot in exc.get("skip_required", []):
                required.discard(slot)
        except Exception as e:
            logger.debug("Character-Exception fuer %s nicht lesbar: %s",
                         character_name, e)

    # Activity-Force: kann Slots hinzufuegen oder entfernen
    if activity_force_required:
        required.update(activity_force_required)
    if activity_force_skip:
        for slot in activity_force_skip:
            required.discard(slot)

    # Runtime-Force-Skip hat hoechste Prio
    if runtime_force_skip:
        for slot in runtime_force_skip:
            required.discard(slot)

    return required





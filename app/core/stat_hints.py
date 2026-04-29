"""Stat-Hint-Aufloesung aus Character-Template.

Liest hint_thresholds-Felder aus dem Character-Template und liefert kurze
Hint-Strings (z.B. "mutig", "erschoepft") basierend auf aktuellen Status-
Effect-Werten. Ersetzt den frueher hardcodierten courage/attention/stamina-
Block in random_events.py.

Template-Format pro Stat-Feld (optional):
    "hint_thresholds": [
        {"min": 70, "text": "mutig"},
        {"max": 30, "text": "aengstlich"}
    ]
"""
from typing import Any, Dict, Iterable, List

from app.core.log import get_logger

logger = get_logger("stat_hints")


def _iter_template_fields(template: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for section in template.get("sections", []) or []:
        for field in section.get("fields", []) or []:
            yield field


def get_stat_hints(character_name: str) -> List[str]:
    """Liefert Hint-Strings fuer den Character anhand der aktuellen Stats.

    Iteriert ueber alle Stat-Felder im Character-Template (store=status_effects),
    wertet deren hint_thresholds gegen den aktuellen Wert aus und sammelt die
    zutreffenden Texte. Stats ohne hint_thresholds werden uebersprungen.
    """
    try:
        from app.models.character import get_character_profile
        from app.models.character_template import get_template
    except Exception:
        return []

    profile = get_character_profile(character_name) or {}
    status = profile.get("status_effects", {}) or {}
    template_name = profile.get("template", "") or ""
    if not template_name:
        return []
    template = get_template(template_name)
    if not template:
        return []

    hints: List[str] = []
    for field in _iter_template_fields(template):
        if field.get("store") != "status_effects":
            continue
        thresholds = field.get("hint_thresholds") or []
        if not thresholds:
            continue
        key = field.get("key")
        if not key:
            continue
        default = field.get("default", 100)
        try:
            value = int(status.get(key, default))
        except (TypeError, ValueError):
            continue
        for t in thresholds:
            if not isinstance(t, dict):
                continue
            text = (t.get("text") or "").strip()
            if not text:
                continue
            if "min" in t and value >= int(t["min"]):
                hints.append(text)
            elif "max" in t and value <= int(t["max"]):
                hints.append(text)
    return hints


def format_character_with_hints(character_name: str) -> str:
    """Wie get_stat_hints, aber formatiert als 'Name (hint1, hint2)'.

    Liefert nur den Namen zurueck, wenn keine Hints zutreffen.
    """
    hints = get_stat_hints(character_name)
    return f"{character_name} ({', '.join(hints)})" if hints else character_name

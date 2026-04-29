"""Avatar-Activity-Erkennung aus User-Nachrichten.

Keyword-basierte Heuristik analog zu avatar_mood_detect. Sucht nach
expliziten Intent-Phrasen ("ich gehe jetzt X", "ich mache X", ...) und
matched X gegen die verfuegbaren Activities am aktuellen Ort.

Absichtlich konservativ: Fragen ("willst du kochen?") und Konjunktive
("ich wuerde gerne") werden nicht gematcht.
"""
import re
from typing import Optional

from app.core.log import get_logger

logger = get_logger("avatar_activity")


# Intent-Phrasen — nur klare Aussagen in 1. Person, Praesens/Zukunft.
# Die Gruppe (.+?) faengt den Activity-Kandidaten.
_INTENT_PATTERNS = [
    r"\bich gehe (?:jetzt |gleich |mal )?(.+?)(?:[.!?,;]|$)",
    r"\bich mach(?:e|'|e ja)? (?:jetzt |gleich |mal )?(.+?)(?:[.!?,;]|$)",
    r"\bich werde (?:jetzt |gleich |mal )?(.+?)(?:[.!?,;]|$)",
    r"\bich bin (?:am|beim) (.+?)(?:[.!?,;]|$)",
    r"\bi(?:'m| am) (?:going to |gonna )(.+?)(?:[.!?,;]|$)",
    r"\bi(?:'m| am) (.+?ing)(?:[.!?,;]|$)",
]

# Disqualifier — wenn die Nachricht eine Frage ist oder einen Konjunktiv
# enthaelt, keine Aenderung (User fragt, bestellt nicht).
_NEGATIVE_HINTS = (
    " würde", " wuerde", " wuerdest", " koennte", " könnte", " sollte",
    " would", " could", " should", "?")


def detect_avatar_activity(
    user_message: str,
    available_activity_names: list,
    current_activity: str = "") -> Optional[str]:
    """Erkennt aus der User-Nachricht eine gewuenschte Aktivitaet.

    Args:
        user_message: Die gerade abgeschickte Nachricht.
        available_activity_names: Liste aller Activity-Namen (Library +
            Location + Character) am aktuellen Ort des Avatars. Wird
            als Whitelist genutzt — Freitext wird NICHT akzeptiert.
        current_activity: Aktuelle Aktivitaet — wird nicht neu gesetzt
            wenn gleich.

    Returns:
        Activity-Name (aus der Whitelist) wenn eindeutig erkannt, sonst None.
    """
    if not user_message or not available_activity_names:
        return None
    text = user_message.lower().strip()

    # Fragen / Konjunktive ausschliessen
    for neg in _NEGATIVE_HINTS:
        if neg in text:
            return None

    # Whitelist vorbereiten (case-insensitive Match)
    name_map = {}
    for n in available_activity_names:
        nl = (n or "").strip().lower()
        if nl:
            name_map[nl] = n

    # Patterns durchlaufen
    for pat in _INTENT_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        candidate = m.group(1).strip()
        # Substring-Match gegen die Whitelist
        for key, original in name_map.items():
            if key in candidate or candidate in key:
                if original.strip().lower() == (current_activity or "").strip().lower():
                    return None
                logger.debug("Avatar activity detected: '%s' via candidate '%s'",
                             original, candidate)
                return original
    return None

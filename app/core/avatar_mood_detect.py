"""Avatar-Mood-Erkennung aus User-Nachrichten.

Leichte Keyword-basierte Heuristik — kein LLM-Call. Hook im Chat-Endpoint
updated `current_feeling` des Avatar, wenn der User in seiner Nachricht
eindeutig ein Gefuehl aussert.

Bewusst konservativ: keine Aenderung wenn Signal schwach oder bereits
passend.
"""
import re
from typing import Optional

from app.core.log import get_logger

logger = get_logger("avatar_mood")


# Reihenfolge wichtig — erster Match gewinnt. Staerkste/spezifischste Woerter zuerst.
# Jedes Tuple: (mood_label, [Wort-Patterns]).
# Pattern werden als \b...\b (Word-Boundary) gematcht.
_MOOD_PATTERNS = [
    ("erschoepft", [
        "muede", "müde", "kaputt", "erschoepft", "erschöpft",
        "am ende", "fix und fertig", "fertig mit der welt",
        "tired", "exhausted", "burnt out", "worn out",
    ]),
    ("wuetend", [
        "wuetend", "wütend", "sauer", "stocksauer",
        "nervt mich", "geht mir auf den keks", "reg mich auf",
        "angry", "pissed", "furious", "annoyed",
    ]),
    ("traurig", [
        "traurig", "deprimiert", "niedergeschlagen", "down", "bedrueckt",
        "mir geht's schlecht", "heulen",
        "sad", "depressed", "miserable",
    ]),
    ("aengstlich", [
        "aengstlich", "ängstlich", "angst", "panik", "nervoes", "nervös",
        "beunruhigt", "sorge", "sorgen",
        "anxious", "nervous", "worried", "scared",
    ]),
    ("gestresst", [
        "gestresst", "stress", "unter strom", "unter druck",
        "zu viel", "ueberfordert", "überfordert",
        "stressed", "overwhelmed",
    ]),
    ("freudig", [
        "freue mich", "freu mich", "glueck", "glücklich", "gluecklich",
        "yay", "juhu", "genial", "grossartig", "großartig",
        "super", "mega gut", "mega glücklich", "richtig gut",
        "happy", "excited", "thrilled", "delighted",
    ]),
    ("entspannt", [
        "entspannt", "chillig", "relaxed", "ruhig",
        "alles gut", "passt alles", "fuehl mich wohl", "fühl mich wohl",
        "relaxed", "calm", "at ease",
    ]),
    ("verliebt", [
        "verliebt", "himmel auf erden", "schmetterlinge im bauch",
        "in love", "smitten",
    ]),
    ("gelangweilt", [
        "gelangweilt", "langweilig", "oede", "öde", "fad",
        "bored", "boring",
    ]),
]


def detect_avatar_mood(user_message: str, current_feeling: str = "") -> Optional[str]:
    """Erkennt die Stimmung des Users aus seiner Nachricht.

    Args:
        user_message: Die gerade abgeschickte Nachricht
        current_feeling: Aktuelles current_feeling des Avatars — wenn der
            detektierte Mood gleich ist, wird None zurueckgegeben (keine
            unnoetige Aktualisierung).

    Returns:
        mood_label (str) wenn erkannt und sich vom aktuellen unterscheidet,
        None sonst.
    """
    if not user_message or not user_message.strip():
        return None
    text = user_message.lower()
    # Punctuation wegnormalisieren damit "nervt!" gematcht wird
    text = re.sub(r"[!?.,;:]", " ", text)

    for mood, patterns in _MOOD_PATTERNS:
        for p in patterns:
            # Simple Substring-Check reicht hier (Word-Boundary waere fuer
            # mehrteilige Phrasen "nervt mich" unhandlich).
            if p in text:
                if (current_feeling or "").strip().lower() == mood:
                    return None
                logger.debug("Mood detected: '%s' via pattern '%s'", mood, p)
                return mood
    return None

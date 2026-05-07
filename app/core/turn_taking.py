"""Turn-Taking Engine for Group Chat.

Determines which characters respond to a user message and in what order.
Pure scoring module — no LLM calls.

Configurable via .env:
  GROUP_CHAT_THRESHOLD      — Score threshold to respond (default: 2.0)
  GROUP_CHAT_MIN_RESPONDERS — Minimum responders per round (default: 1)
  GROUP_CHAT_MAX_RESPONDERS — Maximum responders per round (default: 3)
  GROUP_CHAT_MENTION_BOOST  — Score boost for @mentioned characters (default: 5.0)
  GROUP_CHAT_COOLDOWN       — Score penalty for last responder (default: 2.0)
"""
import os
import random
import re
from typing import Any, Dict, List, Tuple

from app.core.log import get_logger

logger = get_logger("turn_taking")

# Defaults (overridable via .env)
DEFAULT_THRESHOLD = float(os.environ.get("GROUP_CHAT_THRESHOLD", "2.0"))
DEFAULT_MIN_RESPONDERS = int(os.environ.get("GROUP_CHAT_MIN_RESPONDERS", "1"))
DEFAULT_MAX_RESPONDERS = int(os.environ.get("GROUP_CHAT_MAX_RESPONDERS", "3"))
DEFAULT_MENTION_BOOST = float(os.environ.get("GROUP_CHAT_MENTION_BOOST", "5.0"))
DEFAULT_COOLDOWN = float(os.environ.get("GROUP_CHAT_COOLDOWN", "2.0"))


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """Simple keyword overlap score between two texts."""
    if not text_a or not text_b:
        return 0.0
    stop = {
        "ich", "du", "er", "sie", "es", "wir", "ihr", "und", "oder", "der",
        "die", "das", "ein", "eine", "ist", "hat", "war", "wird", "the",
        "a", "an", "is", "was", "are", "i", "you", "he", "she", "it",
        "we", "they", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "nicht", "aber", "auch", "dann", "wenn", "so",
    }
    words_a = set(text_a.lower().split()) - stop
    words_b = set(text_b.lower().split()) - stop
    if not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_b)


def calculate_response_scores(
    user_message: str,
    participants: List[str],
    chat_context: List[Dict[str, Any]], min_responders: int = None,
    max_responders: int = None,
    threshold: float = None) -> Tuple[List[str], List[str]]:
    """Score each participant and return (responders, passive_characters).

    Returns:
        Tuple of (responder_names_in_order, passive_character_names)
    """
    # Apply defaults from .env
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    if min_responders is None:
        min_responders = DEFAULT_MIN_RESPONDERS
    if max_responders is None:
        max_responders = DEFAULT_MAX_RESPONDERS

    if not participants:
        return [], []

    # Detect direct @mentions or name mentions
    msg_lower = user_message.lower()
    mentioned = set()
    for name in participants:
        # @Name or just Name as whole word
        if re.search(r'@' + re.escape(name.lower()), msg_lower):
            mentioned.add(name)
        elif re.search(r'\b' + re.escape(name.lower()) + r'\b', msg_lower):
            mentioned.add(name)

    # Find last responder for cooldown
    last_responder = ""
    for msg in reversed(chat_context):
        if msg.get("role") == "assistant" and msg.get("character"):
            last_responder = msg["character"]
            break

    # Load personality texts for topic relevance (lazy, cached per call)
    _personality_cache: Dict[str, str] = {}

    def _get_personality(char_name: str) -> str:
        if char_name not in _personality_cache:
            try:
                from app.models.character import get_character_profile
                profile = get_character_profile(char_name)
                _personality_cache[char_name] = profile.get("character_personality", "")
            except Exception:
                _personality_cache[char_name] = ""
        return _personality_cache[char_name]

    # Load relationship strength
    def _get_strength(char_name: str) -> float:
        try:
            from app.models.relationship import get_relationship
            from app.models.account import get_active_character
            avatar = (get_active_character() or "").strip()
            if not avatar:
                # Kein Avatar aktiv → keine Avatar-Beziehung lookup-bar.
                # Default-Score; Sentinel "Player" wuerde nichts matchen
                # und nur als Pseudo-Charakter durchs System wandern.
                return 10.0
            rel = get_relationship(char_name, avatar)
            if rel:
                return rel.get("strength", 10)
        except Exception:
            pass
        return 10.0

    scores: Dict[str, float] = {}

    for name in participants:
        score = 0.0

        # 1. Direct mention — always respond
        if name in mentioned:
            score += DEFAULT_MENTION_BOOST

        # 2. Topic relevance (0-3)
        personality = _get_personality(name)
        if personality and user_message:
            overlap = _keyword_overlap(personality, user_message)
            score += min(3.0, overlap * 6.0)

        # 3. Relationship strength (0-2)
        strength = _get_strength(name)
        score += min(2.0, strength / 50.0)

        # 4. Talkativeness (0-1) — from config or default
        try:
            from app.models.character import get_character_config
            config = get_character_config(name)
            talkativeness = float(config.get("talkativeness", 0.5))
        except Exception:
            talkativeness = 0.5
        score += min(1.0, max(0.0, talkativeness))

        # 5. Cooldown (if last responder)
        if name == last_responder:
            score -= DEFAULT_COOLDOWN

        # 6. Random jitter (0-1) for natural feel
        score += random.random()

        scores[name] = score

    # Sort by score descending
    sorted_chars = sorted(scores.items(), key=lambda x: -x[1])

    # Select responders
    # Mentioned characters always respond
    responders = []
    for name, sc in sorted_chars:
        if name in mentioned:
            responders.append(name)
        elif sc >= threshold and len(responders) < max_responders:
            responders.append(name)

    # Ensure minimum responders
    if len(responders) < min_responders:
        for name, sc in sorted_chars:
            if name not in responders:
                responders.append(name)
                if len(responders) >= min_responders:
                    break

    # Cap at max
    responders = responders[:max_responders]

    # Passive = everyone else
    passive = [name for name in participants if name not in responders]

    logger.info(
        "Turn-taking: %d participants, %d responders %s, scores=%s",
        len(participants), len(responders), responders,
        {n: f"{s:.1f}" for n, s in scores.items()})

    return responders, passive

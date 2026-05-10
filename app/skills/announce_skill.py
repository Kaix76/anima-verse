"""Announce Skill — One-way Broadcast an alle Anwesenden.

Im Unterschied zu TalkTo (1:1, mit Antwort) und SendMessage (asynchron, mit
Antwort) ist Announce **reine Perception**: Empfaenger bekommen einen
Memory-Eintrag plus AgentLoop-Bump, antworten aber NICHT automatisch im
Chat. Der naechste Loop-Tick rendert fuer jeden Empfaenger das
``tasks/perceive_announcement.md``-Template — der Charakter verarbeitet
das Event innerlich (Intent / Memory), ohne Reply.

Beispiele:
  - "Heute Abend Party bei mir um 19 Uhr!" (alle im Raum)
  - Mit `scope=location` erreicht der Sender den ganzen Ort, nicht nur
    den eigenen Raum.

Input-Format (JSON via Tool-LLM):
  {"text": "...", "scope": "here|location"}

Spaeter geplant: scope=radius:N, scope=world, scope=to=group_name.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("announce")


SENDER_COOLDOWN_MIN = 5
RECIPIENT_DEDUP_MIN = 30
RECIPIENT_CAP = 30


class AnnounceSkill(BaseSkill):
    """Verkuendet eine Nachricht an alle Anwesenden im Scope.

    Reine Perception: Empfaenger bekommen Memory + Bump, kein Chat-Reply.
    """

    SKILL_ID = "announce"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("announce")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": True}
        logger.info("Announce Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Announce Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        sender_name = (ctx.get("agent_name") or "").strip()
        if not sender_name:
            return "Error: sender context missing."

        text, scope = _extract_text_and_scope(ctx)
        if not text:
            return "Error: empty announcement text."
        if scope not in ("here", "location"):
            scope = "here"

        if _sender_on_cooldown(sender_name, scope):
            return (f"You already made an announcement very recently — "
                    f"wait at least {SENDER_COOLDOWN_MIN} minutes before the next one.")

        recipients = resolve_recipients(scope, sender_name)
        if not recipients:
            return "Announcement made, but nobody is around to hear it."

        delivered: List[str] = []
        skipped_dedup: List[str] = []
        for recipient in recipients[:RECIPIENT_CAP]:
            if _recipient_recently_perceived(recipient, sender_name, text):
                skipped_dedup.append(recipient)
                continue
            _record_perception(recipient, sender_name, text, scope)
            _bump_with_perception(recipient, sender_name, text, scope)
            delivered.append(recipient)

        # Sender's own diary entry (so the sender remembers what they said).
        try:
            from app.models.memory import add_memory
            scope_label = "the whole location" if scope == "location" else "this room"
            add_memory(
                sender_name,
                f"Announced to {scope_label}: \"{text}\"",
                tags=["announcement_sent",
                      f"announcement_sent:{scope}"],
                importance=3)
        except Exception as e:
            logger.debug("Sender memory failed: %s", e)

        names_part = ", ".join(delivered) if delivered else "no one"
        suffix = ""
        if skipped_dedup:
            suffix = f" ({len(skipped_dedup)} already heard a similar message recently)"
        logger.info(
            "Announce by %s (scope=%s) → delivered=%d skipped_dedup=%d text=%r",
            sender_name, scope, len(delivered), len(skipped_dedup), text[:120])
        return (f"Announcement made. {len(delivered)} present heard it: "
                f"{names_part}.{suffix}")

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name,
                              '{"text": "Party tonight at my place — 7pm!", "scope": "here"}')

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Input JSON: {{\"text\": \"...\", \"scope\": \"here\"|\"location\"}}. "
                f"No reply will come back — this is a one-way broadcast."
            ),
            func=self.execute)


# ---------------------------------------------------------------------------
# Helpers — recipients, cooldown, dedup, delivery
# ---------------------------------------------------------------------------


def resolve_recipients(scope: str, sender: str) -> List[str]:
    """Return character names within the announcement scope, excluding sender.

    scope=here     -> same location AND same room as sender
    scope=location -> same location, all rooms
    """
    from app.models.character import (
        get_character_current_location,
        get_character_current_room)

    sender_loc = (get_character_current_location(sender) or "").strip()
    if not sender_loc:
        return []
    sender_room = (get_character_current_room(sender) or "").strip()

    if scope == "here":
        from app.core.room_entry import _list_characters_in_room
        return _list_characters_in_room(
            sender_loc, sender_room, exclude=sender)

    # scope=location → all chars at the same location, any room
    from app.models.character import list_available_characters
    out: List[str] = []
    for c in list_available_characters():
        if c == sender:
            continue
        if (get_character_current_location(c) or "").strip() == sender_loc:
            out.append(c)
    return out


def _sender_on_cooldown(sender: str, scope: str) -> bool:
    """True if the sender announced (any text, this scope) within the
    cooldown window. Prevents the tool-LLM from spamming announcements."""
    try:
        from app.models.memory import load_memories
        cutoff = (datetime.now() - timedelta(minutes=SENDER_COOLDOWN_MIN)).isoformat()
        target_tag = f"announcement_sent:{scope}"
        for m in load_memories(sender):
            ts = m.get("timestamp") or ""
            if ts < cutoff:
                continue
            if target_tag in (m.get("tags") or []):
                return True
    except Exception as e:
        logger.debug("Sender cooldown check failed: %s", e)
    return False


def _recipient_recently_perceived(recipient: str, sender: str, text: str) -> bool:
    """True if this recipient already perceived the same announcement text
    from this sender within the dedup window."""
    try:
        from app.models.memory import load_memories
        cutoff = (datetime.now() - timedelta(minutes=RECIPIENT_DEDUP_MIN)).isoformat()
        target_tag = f"announcement_heard:{sender}"
        text_norm = text.strip().lower()
        for m in load_memories(recipient):
            ts = m.get("timestamp") or ""
            if ts < cutoff:
                continue
            tags = m.get("tags") or []
            if target_tag not in tags:
                continue
            if (m.get("content") or "").strip().lower().endswith(text_norm + '"'):
                return True
    except Exception as e:
        logger.debug("Recipient dedup check failed: %s", e)
    return False


def _record_perception(recipient: str, sender: str, text: str, scope: str) -> None:
    """Memory entry on the recipient — this is what the perceive prompt
    can later refer to and what daily summaries will roll up."""
    try:
        from app.models.memory import add_memory
        add_memory(
            recipient,
            f"Heard {sender} announce: \"{text}\"",
            tags=["announcement_heard",
                  f"announcement_heard:{sender}",
                  f"announcement_scope:{scope}"],
            importance=3,
            related_character=sender)
    except Exception as e:
        logger.debug("Recipient perception memory failed for %s: %s", recipient, e)


def _bump_with_perception(recipient: str, sender: str, text: str, scope: str) -> None:
    """Queue the recipient for a focused perception turn on the next slot.

    Avatars are filtered out by AgentLoop._is_agent_eligible (player-
    controlled chars do not auto-think). For everyone else: a perception
    payload that swaps the system prompt to perceive_announcement and
    locks tools to a tight whitelist (no TalkTo/SendMessage back).
    """
    try:
        from app.core.agent_loop import get_agent_loop
        from app.models.relationship import get_relationship
    except Exception as e:
        logger.debug("Announce bump imports failed: %s", e)
        return

    relationship_hint = ""
    try:
        rel = get_relationship(recipient, sender) or {}
        sentiment = rel.get("sentiment")
        strength = rel.get("strength")
        bits = []
        if isinstance(sentiment, str) and sentiment:
            bits.append(sentiment)
        if isinstance(strength, (int, float)):
            if strength >= 70:
                bits.append("close")
            elif strength <= 30:
                bits.append("distant")
        if bits:
            relationship_hint = ", ".join(bits)
    except Exception:
        pass

    sender_location_name = ""
    sender_room_name = ""
    try:
        from app.models.character import (
            get_character_current_location,
            get_character_current_room)
        from app.models.world import (
            get_location_name, get_location_by_id, get_room_by_id)

        sender_loc_id = (get_character_current_location(sender) or "").strip()
        if sender_loc_id:
            sender_location_name = get_location_name(sender_loc_id) or sender_loc_id
            sender_room_id = (get_character_current_room(sender) or "").strip()
            if sender_room_id:
                loc_obj = get_location_by_id(sender_loc_id)
                if loc_obj:
                    room_obj = get_room_by_id(loc_obj, sender_room_id)
                    if room_obj:
                        sender_room_name = room_obj.get("name", "") or ""
    except Exception as e:
        logger.debug("Announce sender-location lookup failed: %s", e)

    perception_vars = {
        "announcement_sender": sender,
        "announcement_text": text,
        "announcement_scope": scope,
        "relationship_to_sender": relationship_hint,
        "announcement_sender_location": sender_location_name,
        "announcement_sender_room": sender_room_name,
    }

    try:
        get_agent_loop().bump(
            recipient,
            perception_template="tasks/perceive_announcement.md",
            perception_vars=perception_vars,
            tool_whitelist=["SetLocation"])
    except Exception as e:
        logger.debug("Announce bump failed for %s: %s", recipient, e)


def _extract_text_and_scope(ctx: Dict[str, Any]) -> tuple:
    """Pull text + scope out of the JSON tool-input. Falls back to plain
    string handling for very simple call shapes."""
    text = ""
    scope = "here"

    # Fields directly merged from the JSON tool call
    if isinstance(ctx.get("text"), str):
        text = ctx["text"].strip()
    if isinstance(ctx.get("scope"), str):
        scope = ctx["scope"].strip().lower() or "here"

    if text:
        return text, scope

    # Fallback: input is a JSON string with text/scope
    raw = ctx.get("input") or ""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                text = (parsed.get("text") or "").strip()
                scope_v = (parsed.get("scope") or "").strip().lower()
                if scope_v:
                    scope = scope_v
        except Exception:
            pass

    # Last fallback: raw input is plain text
    if not text and isinstance(raw, str):
        text = raw.strip()

    return text, scope

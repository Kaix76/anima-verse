"""TalkTo Skill — Face-to-face Nachricht an einen anderen Character.

Nur verfuegbar, wenn der Ziel-Character am gleichen Ort ist wie der Sender.
Fuer Fernkommunikation siehe SendMessage Skill.

Input-Format: "CharacterName, Nachricht"
Beispiel: "Pixel, kannst du mir kurz helfen?"
"""
import os
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("talk_to")

from app.models.character import list_available_characters


class TalkToSkill(BaseSkill):
    """Ein Character spricht am gleichen Ort mit einem anderen.

    Das System ruft das LLM des Ziel-Characters und speichert beide
    Seiten der Konversation in der Chat-History. Die Antwort des Ziels
    wird als Skill-Output zurueckgegeben — der anrufende Character
    sieht damit, wie reagiert wurde.
    """

    SKILL_ID = "talk_to"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_TALK_TO_NAME", "TalkTo")
        self.description = os.environ.get(
            "SKILL_TALK_TO_DESCRIPTION",
            "Speak face-to-face to a THIRD character who is NOT part of the current "
            "conversation but IS at your current location. NEVER use this to address the "
            "user or the character you are already chatting with — they already receive "
            "your words through the RP itself. Also do NOT use for remote contact "
            "(different location) — use SendMessage instead."
        )
        self._defaults = {"enabled": True}
        logger.info("TalkTo Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "TalkTo Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        input_text = ctx.get("input", raw_input).strip()
        sender_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not sender_name:
            return "Error: sender context missing."
        if not input_text:
            return "Error: empty input. Format: 'CharacterName, message'"

        # Parse "Target, message"
        parts = input_text.split(",", 1)
        if len(parts) < 2 or not parts[1].strip():
            parts = input_text.split(" ", 1)
        target_raw = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else ""
        if not message:
            return f"Error: no message for {target_raw}."

        # Resolve target name
        available = list_available_characters()
        target_name = _resolve_name(target_raw, available)
        if not target_name:
            return f"Character '{target_raw}' not found. Available: {', '.join(available)}"
        if target_name == sender_name:
            return "You cannot talk to yourself."

        # Chat-Partner-Check: TalkTo ist fuer DRITTE — der aktuelle Chat-Partner
        # empfaengt Aussagen direkt durchs RP, TalkTo-Aufruf waere redundant.
        try:
            from app.models.account import get_chat_partner
            current_partner = (get_chat_partner() or "").strip()
            if current_partner and target_name == current_partner:
                return (
                    f"{target_name} is already in the current conversation — "
                    f"address them directly through your RP speech, not via TalkTo. "
                    f"TalkTo is only for third characters at your location."
                )
        except Exception:
            pass

        # Location-Check: muss am gleichen Ort sein
        from app.models.character import get_character_current_location
        self_loc = get_character_current_location(sender_name) or ""
        target_loc = get_character_current_location(target_name) or ""
        if not self_loc or self_loc != target_loc:
            return (
                f"{target_name} is not at your location. "
                f"Use SendMessage for remote contact."
            )

        # Sleep / Busy-Check
        from app.models.character import is_character_sleeping
        if is_character_sleeping(target_name):
            return f"{target_name} is sleeping and cannot be reached."
        from app.core.activity_engine import is_character_interruptible
        can_interrupt, busy = is_character_interruptible(target_name)
        if not can_interrupt:
            return f"{target_name} is focused on '{busy}' and cannot be interrupted right now."

        logger.info("TalkTo %s -> %s: %s", sender_name, target_name, message[:100])

        # Pending-Report anlegen wenn der Skill aus einem Chat mit jemand anderem
        # getriggert wurde (Auftraggeber-Kette).
        initiator = ctx.get("initiator", "").strip()
        if initiator and initiator != sender_name:
            try:
                from app.core.pending_reports import add_report
                add_report(
                    reporter=sender_name,
                    initiator=initiator,
                    initiator_type="user" if initiator == "user" else "character",
                    target=target_name,
                    trigger_type="talk_to_response")
            except Exception as e:
                logger.debug("pending_report add failed: %s", e)

        # Ziel-Response via zentrale Chat-Engine
        from app.core.chat_engine import run_chat_turn
        reply = run_chat_turn(
            owner_id=user_id,
            responder=target_name,
            speaker=sender_name,
            incoming_message=message,
            medium="in_person",
            task_type="talk_to")

        # Resolve: falls dieser TalkTo einen offenen pending_report aufloest.
        try:
            from app.core.pending_reports import list_open, mark_resolved
            from app.models.account import get_active_character
            active_avatar = get_active_character() or ""
            for r in list_open(sender_name):
                to_who = r.get("to", "")
                if to_who == target_name or (to_who == "user" and target_name == active_avatar):
                    mark_resolved(sender_name, r["id"])
                    break
        except Exception as e:
            logger.debug("pending_report resolve failed: %s", e)

        if not reply:
            return f"{target_name} did not respond."
        return f"{target_name} replied: {reply}"

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "Pixel, can you help me with this?")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Input: target character name, comma, message. "
                f"Example: 'Pixel, are you free tonight?'. "
                f"Only works when target is at your location; otherwise use SendMessage."
            ),
            func=self.execute)


def _resolve_name(raw: str, available: list) -> str:
    """Case-insensitive + fuzzy Name-Aufloesung."""
    raw_lower = raw.lower()
    for name in available:
        if name.lower() == raw_lower:
            return name
    for name in available:
        if raw_lower in name.lower() or name.lower() in raw_lower:
            return name
    return ""

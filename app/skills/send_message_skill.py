"""SendMessage Skill — Remote-Nachricht an einen anderen Character.

Fuer Fernkommunikation (Character nicht am gleichen Ort). Standard-Medium
ist "messaging" (internes Nachrichtenfenster). Telegram-Zusatz kommt
spaeter als separates Tool oder Parameter.

Input-Format: "CharacterName, Nachricht"
Beispiel: "Luna, bist du heute Abend frei?"
"""
import os
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("send_message")

from app.models.character import list_available_characters


class SendMessageSkill(BaseSkill):
    """Schickt eine Remote-Nachricht an einen anderen Character.

    Unabhaengig vom Ort. Das Ziel-LLM antwortet via zentrale Chat-Engine
    mit medium="messaging". Ergebnis wird als Skill-Output zurueckgegeben.
    """

    SKILL_ID = "send_message"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_SEND_MESSAGE_NAME", "SendMessage")
        self.description = os.environ.get(
            "SKILL_SEND_MESSAGE_DESCRIPTION",
            "Send a remote text message — to another character OR to the user/avatar. "
            "Use this to proactively reach out (e.g. share important news, ask a "
            "question, react to something you observed). The message lands in the "
            "recipient's chat. For in-person conversation in the same room use TalkTo."
        )
        self._defaults = {"enabled": True}
        logger.info("SendMessage Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "SendMessage Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        input_text = ctx.get("input", raw_input).strip()
        sender_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not sender_name:
            return "Error: sender context missing."
        if not input_text:
            return "Error: empty input. Format: 'CharacterName, message'"

        parts = input_text.split(",", 1)
        if len(parts) < 2 or not parts[1].strip():
            parts = input_text.split(" ", 1)
        target_raw = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else ""
        if not message:
            return f"Error: no message for {target_raw}."

        available = list_available_characters()
        target_name = _resolve_name(target_raw, available)
        if not target_name:
            return f"Character '{target_raw}' not found. Available: {', '.join(available)}"
        if target_name == sender_name:
            return "You cannot message yourself."

        # Sleep-Check (busy-Check entfaellt — Messages kann man immer schicken,
        # der Empfaenger liest sie wenn er Zeit hat)
        from app.models.character import is_character_sleeping
        if is_character_sleeping(target_name):
            return f"{target_name} is sleeping — message queued, but no response for now."

        # Avatar-Target erkennen: wenn der Empfaenger der vom User gesteuerte
        # Character ist, gilt SendMessage als "proaktive Chat-Nachricht an den
        # Spieler". Keine Notification (verschmutzt den System-Event-Feed),
        # kein forced_thought (Avatar denkt nicht autonom).
        from app.models.account import is_player_controlled as _is_pc
        target_is_avatar = _is_pc(target_name)

        logger.info("SendMessage %s -> %s: %s", sender_name, target_name, message[:100])

        initiator = ctx.get("initiator", "").strip()
        if initiator and initiator != sender_name:
            try:
                from app.core.pending_reports import add_report
                add_report(
                    reporter=sender_name,
                    initiator=initiator,
                    initiator_type="user" if initiator == "user" else "character",
                    target=target_name,
                    trigger_type="message_response")
            except Exception as e:
                logger.debug("pending_report add failed: %s", e)

        # Asynchron: Nachricht speichern + Notification + forced_thought fuer
        # den Empfaenger einplanen. Der Sender WARTET NICHT auf die Antwort —
        # das ist DM-Semantik (Messaging, nicht Call). Synchron-Warten wuerde
        # bei nested LLM-Queue-Calls den outer Thought-Timeout sprengen.
        from datetime import datetime
        ts = datetime.now().isoformat()

        try:
            from app.models.chat import save_message
            # In Empfaenger-History: Sender als Absender (role=user)
            save_message({
                "role": "user", "content": message, "timestamp": ts,
                "speaker": sender_name, "medium": "messaging",
            }, character_name=target_name, partner_name=sender_name)
            # In Sender-History: Sender als Autor (role=assistant)
            save_message({
                "role": "assistant", "content": message, "timestamp": ts,
                "speaker": sender_name, "medium": "messaging",
            }, character_name=sender_name, partner_name=target_name)
        except Exception as e:
            logger.error("SendMessage: Chat-History speichern fehlgeschlagen: %s", e)

        # Notification nur fuer Character→Character (System-Event-Feed bleibt
        # sauber von "X hat Y geschrieben"-Spam). Bei Avatar-Target reicht der
        # Chat-History-Eintrag — der Chat-Unread-Indikator pingt automatisch.
        if not target_is_avatar:
            try:
                from app.models.notifications import create_notification
                create_notification(
                    character=sender_name,
                    content=f"Nachricht an {target_name}: {message[:500]}",
                    notification_type="message",
                    metadata={"trigger": "send_message", "to": target_name})
            except Exception as e:
                logger.debug("SendMessage: Notification fehlgeschlagen: %s", e)

        # AgentLoop bump: recipient gets prioritised on the next slot so
        # they see the inbox message soon (not waiting for their normal
        # importance quota). Avatars don't think autonomously, so skip.
        if not target_is_avatar:
            try:
                from app.core.agent_loop import get_agent_loop
                get_agent_loop().bump(target_name)
            except Exception as e:
                logger.debug("SendMessage: AgentLoop bump failed: %s", e)

        # Resolve: falls dieser Send einen offenen pending_report aufloest,
        # markieren. Heuristik: Target == initiator eines offenen Reports.
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

        return f"Message sent to {target_name}. They will reply when they get to it."

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "Luna, are you free tonight?")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Input: target character name, comma, message. "
                f"Example: 'Luna, dinner at 8?'. "
                f"For face-to-face use TalkTo instead."
            ),
            func=self.execute)


def _resolve_name(raw: str, available: list) -> str:
    raw_lower = raw.lower()
    for name in available:
        if name.lower() == raw_lower:
            return name
    for name in available:
        if raw_lower in name.lower() or name.lower() in raw_lower:
            return name
    return ""

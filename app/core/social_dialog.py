"""Social Dialog — Begegnungs-Trigger.

Reduces to an AgentLoop bump for the sender. The sender's next thought
turn sees the other character in their presence block (loaded by
``thought_context._build_presence``-equivalent via memory/state) and
decides organically whether to TalkTo or not.

The handler stays registered under task_type "social_dialog" so existing
schedulers / random_events that submit it keep working.
"""
from typing import Dict, Any

from app.core.log import get_logger
logger = get_logger("social_dialog")


def _handle_social_dialog(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bumps the sender so they consider the encounter on their next slot.

    Payload: {sender, target, location}
    """
    from app.models.character import is_character_sleeping
    from app.models.account import is_player_controlled

    sender = payload.get("sender", "")
    target = payload.get("target", "")

    if not (sender and target):
        return {"skipped": True, "reason": "Missing payload fields"}

    if is_player_controlled(sender):
        return {"skipped": True, "reason": "player-controlled"}

    if is_character_sleeping(sender) or is_character_sleeping(target):
        return {"skipped": True, "reason": "sleeping"}

    try:
        from app.core.agent_loop import get_agent_loop
        get_agent_loop().bump(sender)
        logger.info("SocialDialog -> AgentLoop bump: %s meets %s", sender, target)
        return {"success": True, "bumped": sender}
    except Exception as e:
        logger.error("SocialDialog bump failed: %s", e)
        return {"success": False, "error": str(e)}


def register_social_dialog_handler():
    """Registriert den Social-Dialog-Handler bei der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    get_background_queue().register_handler("social_dialog", _handle_social_dialog)

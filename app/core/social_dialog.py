"""Social Dialog — Begegnungs-Trigger fuer forcierte Gedanken.

Ehemals eigener Dialog-Mechanismus mit eigenem Prompt + History.
**Neu**: triggert nur noch einen forcierten Gedanken-Tick fuer den Sender mit
context_hint ueber die Begegnung. Der Sender entscheidet via Tool-Call
(TalkTo / SendMessage / nichts), wie er reagieren will. Antworten landen in
der normalen Chat-History — keine separate social_dialog_history mehr.

Der Handler bleibt unter task_type "social_dialog" registriert, damit der
Scheduler / random_events ihn weiter triggern koennen.
"""
from typing import Dict, Any

from app.core.log import get_logger
logger = get_logger("social_dialog")


def _handle_social_dialog(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Triggert einen forcierten Gedanken fuer den Sender ueber das Treffen.

    Payload: {user_id, sender, target, location}
    """
    from app.models.character import is_character_sleeping
    from app.models.account import is_player_controlled
    from app.models.world import get_location_name

    user_id = payload.get("user_id", "")
    sender = payload.get("sender", "")
    target = payload.get("target", "")
    location_id = payload.get("location", "")

    if not (user_id and sender and target):
        return {"skipped": True, "reason": "Missing payload fields"}

    if is_player_controlled(sender):
        logger.info("SocialDialog skip: %s ist player-controlled", sender)
        return {"skipped": True, "reason": "player-controlled"}

    if is_character_sleeping(sender) or is_character_sleeping(target):
        logger.info("SocialDialog skip: %s oder %s schlaeft", sender, target)
        return {"skipped": True, "reason": "sleeping"}

    location_name = get_location_name(location_id) if location_id else "hier"

    context_hint = (
        f"Du triffst gerade {target} hier in {location_name}. "
        f"Willst du mit ihm/ihr reden? Wenn ja, nutze das TalkTo-Tool. "
        f"Wenn nicht, beschreibe was du stattdessen tust."
    )

    try:
        from app.core.background_queue import get_background_queue
        get_background_queue().submit(
            "forced_thought",
            {
                "user_id": user_id,
                "character_name": sender,
                "context_hint": context_hint,
            })
        logger.info("SocialDialog -> forced_thought: %s trifft %s @ %s",
                    sender, target, location_name)
        return {"success": True, "delegated_to": "forced_thought"}
    except Exception as e:
        logger.error("SocialDialog Trigger Fehler: %s", e)
        return {"success": False, "error": str(e)}


def register_social_dialog_handler():
    """Registriert den Social-Dialog-Handler bei der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    get_background_queue().register_handler("social_dialog", _handle_social_dialog)

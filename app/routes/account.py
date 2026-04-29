"""Account routes — character selection and switching.

Separates account management (which character to control) from auth (login/register).
"""
from fastapi import APIRouter, HTTPException, Request
from typing import Any, Dict

from app.core.log import get_logger
from app.models.account import (
    get_active_character,
    get_default_character,
    set_active_character)
from app.models.character import list_available_characters

logger = get_logger("account")

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/characters")
async def list_characters(request: Request) -> Dict[str, Any]:
    """List characters the player can control (Avatar-faehige Characters).

    Chatbots (Template ohne playable_avatar Flag) werden ausgefiltert —
    sie existieren als NPCs aber koennen nicht der Spieler-Avatar sein.
    Fuer Nicht-Admins zusaetzlich nach allowed_characters filtern.
    """
    from app.core.auth_dependency import filter_characters
    from app.models.character_template import is_feature_enabled

    all_chars = list_available_characters()
    characters = [c for c in all_chars if is_feature_enabled(c, "playable_avatar")]
    characters = filter_characters(request, characters)

    active = get_active_character()
    default = get_default_character()

    return {
        "characters": characters,
        "active_character": active,
        "default_character": default,
    }


@router.post("/switch-character")
async def switch_character(request: Request) -> Dict[str, Any]:
    """Switch which character the player controls.

    The previous character becomes autonomous again; the new one loses autonomy.
    """
    from app.core.auth_dependency import user_can_access_character

    data = await request.json()
    character_name = data.get("character_name", "").strip()

    if not character_name:
        raise HTTPException(status_code=400, detail="character_name required")

    # Zugriffsrecht pruefen (Admin darf alle, User nur allowed_characters)
    if not user_can_access_character(request, character_name):
        raise HTTPException(
            status_code=403,
            detail=f"Kein Zugriff auf Character '{character_name}'")

    # Verify character exists AND is playable as avatar
    available = list_available_characters()
    if character_name not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Character '{character_name}' not found")
    from app.models.character_template import is_feature_enabled
    if not is_feature_enabled(character_name, "playable_avatar"):
        raise HTTPException(
            status_code=400,
            detail=f"Character '{character_name}' ist kein Avatar (Template-Flag playable_avatar=false)")

    previous = get_active_character()
    set_active_character(character_name)

    logger.info(
        "Player %s switched from '%s' to '%s'",
        username, previous or "(none)", character_name)

    return {
        "status": "success",
        "previous_character": previous,
        "active_character": character_name,
    }

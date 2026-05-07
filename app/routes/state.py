"""Aggregated game-state snapshot for the live UI tick.

Bundles the data sources the chat-tab UI needs every few seconds (agent +
avatar mood/activity/location/status_effects/conditions, sidebar at avatar
location, chat-medium hint) into one round-trip. Reduces ~6 separate
calls per tick to a single GET.

Supports ETag-based 304-Not-Modified — when the snapshot hash hasn't
changed since the last response, the body is empty. Clients keep their
existing rendered state.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, Response

from app.core.log import get_logger

logger = get_logger("state")

router = APIRouter(prefix="/state", tags=["state"])


def _build_character_block(name: str) -> Optional[Dict[str, Any]]:
    """Mood/Activity/Location/Conditions/Status fuer einen Character.

    Ruft die existierenden Route-Funktionen direkt auf — kein HTTP-Hop,
    aber dieselbe Logik wie die einzelnen Endpoints.
    """
    if not name or name == "KI":
        return None
    block: Dict[str, Any] = {"name": name}
    # Location / Activity
    try:
        from app.routes.characters import get_character_current_location_route
        loc = get_character_current_location_route(name)
        block.update({
            "location_id": loc.get("current_location_id", "") or "",
            "location_name": loc.get("current_location", "") or "",
            "room_id": loc.get("current_room", "") or "",
            "room_name": loc.get("current_room_name", "") or "",
            "activity": loc.get("current_activity", "") or "",
            "activity_detail": loc.get("current_activity_detail", "") or "",
            "movement_target_id": loc.get("movement_target_id", "") or "",
            "movement_target_name": loc.get("movement_target_name", "") or "",
        })
    except Exception as e:
        logger.debug("state: location fetch failed for %s: %s", name, e)
    # Mood
    try:
        from app.models.character import get_character_current_feeling
        block["mood"] = get_character_current_feeling(name) or ""
    except Exception:
        block["mood"] = ""
    # Status-Effects + Bar-Meta
    try:
        from app.routes.characters import get_status_effects_route
        s = get_status_effects_route(name)
        block["status_effects"] = s.get("status_effects", {}) or {}
        block["bar_meta"] = s.get("bar_meta", {}) or {}
    except Exception:
        block["status_effects"] = {}
        block["bar_meta"] = {}
    # Active Conditions
    try:
        from app.routes.characters import get_active_conditions_route
        c = get_active_conditions_route(name)
        block["conditions"] = c.get("conditions", []) or []
    except Exception:
        block["conditions"] = []
    # Profile-Image-Filename (FE bauet daraus URL)
    try:
        from app.models.character import get_character_profile_image
        block["profile_image"] = get_character_profile_image(name) or ""
    except Exception:
        block["profile_image"] = ""
    return block


def _compute_medium(agent: Optional[Dict[str, Any]],
                    avatar: Optional[Dict[str, Any]]) -> str:
    """Mirror der FE-Logik aus _recomputeChatMedium:
    Beide Locations gesetzt + verschieden -> 'messaging', sonst 'in_person'.
    """
    if not agent:
        return "in_person"
    a_loc = (avatar.get("location_id") if avatar else "") or ""
    p_loc = agent.get("location_id") or ""
    if a_loc and p_loc and a_loc != p_loc:
        return "messaging"
    return "in_person"


@router.get("/snapshot")
async def snapshot(
    agent: str = "",
    avatar: str = "",
    if_none_match: str = Header(default="", alias="If-None-Match"),
) -> Response:
    """Aggregierter UI-State fuer den 3s-Live-Tick.

    Query-Parameter:
        agent  — Chat-Partner (currentCharacterName), leer wenn keiner gewaehlt
        avatar — Player-Character (active_character), leer wenn nicht eingeloggt

    Response (200):
        {
          "agent": {name, mood, activity, location_id, room_id, conditions[],
                    status_effects{}, bar_meta{}, profile_image, ...} | null,
          "avatar": {...same shape...} | null,
          "sidebar": {location_id, location_name,
                      characters: [{name, avatar_url, same_room, room}],
                      chatbots: [{name, avatar_url}]},
          "medium": "in_person" | "messaging"
        }

    Response (304): leer, wenn ETag matcht.
    """
    agent_block = _build_character_block(agent)
    avatar_block = _build_character_block(avatar)

    # Sidebar: Characters am Avatar-Ort + Chatbots
    sidebar: Dict[str, Any] = {
        "location_id": "",
        "location_name": "",
        "characters": [],
        "chatbots": [],
    }
    if avatar_block and avatar_block.get("location_id"):
        try:
            from app.routes.characters import characters_at_location
            at = characters_at_location(
                location=avatar_block["location_id"],
                room=avatar_block.get("room_id", "") or avatar_block.get("room_name", ""),
            )
            sidebar["location_id"] = at.get("location_id", "") or ""
            sidebar["location_name"] = at.get("location", "") or ""
            sidebar["characters"] = at.get("characters", []) or []
        except Exception as e:
            logger.debug("state: at-location fetch failed: %s", e)
    try:
        from app.routes.characters import list_chatbots
        cb = list_chatbots()
        sidebar["chatbots"] = cb.get("characters", []) or []
    except Exception:
        pass

    # Unread-Summary (Chat-Badges) — hier integriert damit der Tick einen
    # einzigen Round-Trip macht.
    unread: Dict[str, Any] = {"avatar": "", "chats": {}}
    try:
        from app.routes.chat import chat_unread_summary
        unread = await chat_unread_summary()
    except Exception as e:
        logger.debug("state: unread-summary fetch failed: %s", e)

    payload: Dict[str, Any] = {
        "agent": agent_block,
        "avatar": avatar_block,
        "sidebar": sidebar,
        "unread": unread,
        "medium": _compute_medium(agent_block, avatar_block),
    }

    # ETag aus serialisiertem Payload — deterministisch, 304 spart Bandbreite
    serialized = json.dumps(payload, sort_keys=True, default=str)
    etag = hashlib.md5(serialized.encode("utf-8")).hexdigest()
    quoted_etag = f'"{etag}"'

    if if_none_match.strip() == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    return JSONResponse(content=payload, headers={"ETag": quoted_etag})

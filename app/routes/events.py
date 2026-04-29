"""Event routes - Situative Ereignisse + Outfit-SSE-Stream"""
import asyncio
import json as _json

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from typing import Dict, Any
from app.core.log import get_logger
from app.models.events import add_event, get_all_events, delete_event

logger = get_logger("events")

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/outfit-stream")
async def outfit_event_stream(request: Request) -> StreamingResponse:
    """SSE-Stream fuer Outfit-Change-Events.

    Multiuser: Filter nach allowed_characters — User sieht nur Events zu
    Characters die ihm zugeordnet sind. Admin mit allowed=[] sieht keine
    (muss sich Characters zuweisen). Unauthenticated: 401.
    """
    from app.core.outfit_events import subscribe
    from app.core.auth_dependency import get_current_user_optional

    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    allowed = set(user.get("allowed_characters") or [])

    async def gen():
        yield f"data: {_json.dumps({'type': 'connected'})}\n\n"
        try:
            async for event in subscribe():
                char = event.get("character", "")
                if char and char not in allowed:
                    continue
                payload = {"type": "outfit_changed", **event}
                yield f"data: {_json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("")
def list_events_route() -> Dict[str, Any]:
    """Listet alle Events eines Users."""
    events = get_all_events()
    return {"events": events}


@router.post("")
async def create_event_route(request: Request) -> Dict[str, Any]:
    """Erstellt ein neues Event."""
    body = await request.json()
    user_id = body.get("user_id", "")
    text = body.get("text", "").strip()
    location_id = body.get("location_id") or None

    ttl_hours = body.get("ttl_hours")
    if ttl_hours is not None:
        ttl_hours = int(ttl_hours)

    if not text:
        raise HTTPException(status_code=400, detail="user_id and text required")

    event = add_event(text, location_id=location_id, ttl_hours=ttl_hours)
    return {"ok": True, "event": event}


@router.delete("/{event_id}")
def delete_event_route(
    event_id: str) -> Dict[str, Any]:
    """Loescht ein Event."""
    deleted = delete_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True}

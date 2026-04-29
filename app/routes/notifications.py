"""Notification API routes."""
from typing import Any, Dict, Optional
from fastapi import APIRouter, Query, HTTPException, Request
from app.core.log import get_logger

logger = get_logger("notifications")

from app.models.notifications import (
    get_notifications,
    get_unread_count,
    mark_read,
    mark_all_read,
    delete_notification)
from app.models.account import get_user_profile, save_user_profile

router = APIRouter(prefix="/notifications")


def _allowed_characters(request: Request) -> Optional[list]:
    """Liefert die allowed_characters des aktuellen Users. None bei keinem Auth
    (treat as unrestricted — nur fuer Legacy; authed User bekommen immer Filter)."""
    from app.core.auth_dependency import get_current_user_optional
    user = get_current_user_optional(request)
    if not user:
        return None
    return list(user.get("allowed_characters") or [])


@router.get("")
def list_notifications(request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False)) -> Dict[str, Any]:
    """List notifications for the current user (newest first, filtered by allowed_characters)."""
    allowed = _allowed_characters(request)
    items = get_notifications(limit=limit, offset=offset, unread_only=unread_only,
                               character_whitelist=allowed)
    unread = get_unread_count(character_whitelist=allowed)
    return {"notifications": items, "unread_count": unread}


@router.get("/unread-count")
def unread_count(request: Request) -> Dict[str, int]:
    """Lightweight polling endpoint — returns only the unread count (user-filtered)."""
    return {"unread_count": get_unread_count(character_whitelist=_allowed_characters(request))}


@router.post("/{notification_id}/read")
def read_notification(notification_id: str) -> Dict[str, Any]:
    """Mark a single notification as read."""
    found = mark_read(notification_id)
    if not found:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


@router.post("/read-all")
def read_all_notifications() -> Dict[str, Any]:
    """Mark all notifications as read."""
    count = mark_all_read()
    return {"success": True, "marked": count}


@router.delete("/{notification_id}")
def remove_notification(notification_id: str) -> Dict[str, Any]:
    """Delete a single notification."""
    found = delete_notification(notification_id)
    if not found:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


@router.get("/style")
def get_style() -> Dict[str, str]:
    """Get the user's notification style preference."""
    profile = get_user_profile()
    return {"style": profile.get("notification_style", "modern")}


@router.put("/style")
async def set_style(request: Request) -> Dict[str, Any]:
    """Set the user's notification style preference."""
    body = await request.json()
    user_id = body.get("user_id", "")
    style = body.get("style", "modern")
    if style not in ("modern", "medieval", "magical"):
        raise HTTPException(status_code=400, detail="Invalid style. Use: modern, medieval, magical")
    profile = get_user_profile()
    profile["notification_style"] = style
    save_user_profile(profile)
    return {"success": True, "style": style}

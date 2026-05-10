"""Routes for the world-level setup / premise.

Single multi-line text field that gets injected as ``world_setup`` into
the chat-stream and World-Dev system prompts. Edited from the Game-Admin
"Setup" tab. Admin-only: only logged-in admins can read or write.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth_dependency import require_admin
from app.models.world_setup import get_world_setup, save_world_setup

router = APIRouter(prefix="/admin/world-setup", tags=["world-setup"],
                   dependencies=[Depends(require_admin)])


@router.get("")
async def get_world_setup_route():
    """Return the current world setup (always a dict with `description`)."""
    return get_world_setup()


@router.put("")
async def save_world_setup_route(request: Request):
    """Persist the world setup. Body: ``{"description": "..."}``."""
    body = await request.json()
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        raise HTTPException(status_code=400, detail="description must be a string")
    return save_world_setup(description or "")

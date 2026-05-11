"""Routes for the per-world Storyteller configuration.

Single JSON config that controls which skills the act-skill pipeline may
trigger, in which chat mode the StreamingAgent runs, and which LLM-task
is routed. Edited from the Game-Admin "Storyteller" tab. Admin-only.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth_dependency import require_admin
from app.models.storyteller import (
    get_storyteller_config, list_skill_keys, save_storyteller_config,
)

router = APIRouter(prefix="/admin/storyteller", tags=["storyteller"],
                   dependencies=[Depends(require_admin)])


@router.get("/config")
async def get_config_route():
    """Return the current storyteller config plus the full list of known
    skill keys (so the UI can render unknown skills consistently)."""
    return {
        "config": get_storyteller_config(),
        "skill_keys": list_skill_keys(),
    }


@router.put("/config")
async def save_config_route(request: Request):
    """Persist the storyteller config. Body shape matches ``get_storyteller_config``.
    Fields are normalised (unknown skill keys dropped, invalid modes
    coerced to the default ``rp_first``).
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    saved = save_storyteller_config(body)
    return {"config": saved, "skill_keys": list_skill_keys()}

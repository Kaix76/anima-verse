"""Assignment routes — CRUD API fuer temporaere Aufgaben."""
from fastapi import APIRouter, HTTPException, Request
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.models.assignments import (
    create_assignment,
    get_assignment,
    list_assignments,
    update_assignment,
    delete_assignment,
    add_progress,
    complete_assignment)

logger = get_logger("assignments_route")

router = APIRouter(prefix="/assignments", tags=["assignments"])


@router.get("/{user_id}")
def get_assignments(character: Optional[str] = None,
    status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List assignments for a user, optionally filtered by character and/or status."""
    return list_assignments(character_name=character, status=status)


@router.get("/{user_id}/{assignment_id}")
def get_single_assignment(assignment_id: str) -> Dict[str, Any]:
    """Get a single assignment by ID."""
    a = get_assignment(assignment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return a


@router.post("/{user_id}")
async def create_new_assignment(request: Request) -> Dict[str, Any]:
    """Create a new assignment.

    Body:
    {
        "title": "Fotoshooting im Park",
        "description": "Enzo fotografiert Kira...",
        "participants": {
            "Enzo": {"role": "Fotograf"},
            "Kira": {"role": "Model"}
        },
        "priority": 2,
        "duration_minutes": 120,   // OR
        "expires_at": "2026-03-31T18:00:00"
    }
    """
    data = await request.json()
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    participants = data.get("participants", {})

    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not participants:
        raise HTTPException(status_code=400, detail="participants is required (at least one character)")

    return create_assignment(
        title=title,
        description=description,
        participants=participants,
        priority=data.get("priority", 3),
        duration_minutes=data.get("duration_minutes"),
        expires_at=data.get("expires_at"),
        location_id=data.get("location_id"),
        outfit_hint=data.get("outfit_hint"))


@router.patch("/{user_id}/{assignment_id}")
async def patch_assignment(assignment_id: str, request: Request
) -> Dict[str, Any]:
    """Update an assignment (partial update)."""
    data = await request.json()
    result = update_assignment(assignment_id, data)
    if not result:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return result


@router.delete("/{user_id}/{assignment_id}")
def remove_assignment(assignment_id: str) -> Dict[str, str]:
    """Delete an assignment."""
    if not delete_assignment(assignment_id):
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"status": "deleted"}


@router.post("/{user_id}/{assignment_id}/progress")
async def post_progress(assignment_id: str, request: Request
) -> Dict[str, Any]:
    """Add progress for a character on an assignment.

    Body: {"character": "Enzo", "note": "Equipment vorbereitet"}
    """
    data = await request.json()
    character = data.get("character", "").strip()
    note = data.get("note", "").strip()

    if not character or not note:
        raise HTTPException(status_code=400, detail="character and note are required")

    result = add_progress(assignment_id, character, note)
    if not result:
        raise HTTPException(status_code=404, detail="Assignment or character not found")
    return result


@router.post("/{user_id}/{assignment_id}/complete")
async def post_complete(assignment_id: str, request: Request
) -> Dict[str, Any]:
    """Mark an assignment as completed.

    Body: {"character": "Enzo", "note": "Alle Fotos fertig!"} (optional)
    """
    data = await request.json()
    character = data.get("character")
    note = data.get("note", "")

    result = complete_assignment(assignment_id, character, note)
    if not result:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return result

"""Relationship / Social Graph API routes."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from app.core.log import get_logger
from app.models.relationship import (
    get_all_relationships,
    get_character_relationships,
    get_relationship,
    update_relationship_manual,
    build_graph_data,
    reclassify_all_relationships,
)

logger = get_logger("routes.relationships")

router = APIRouter(prefix="/relationships", tags=["relationships"])


# ---------------------------------------------------------------------------
# Graph data (for vis-network)
# ---------------------------------------------------------------------------

@router.get("/graph")
async def get_graph(character: Optional[str] = Query(None)):
    """Return nodes + edges for the social graph visualization.

    If *character* is given, only return that character's direct relationships.
    """
    return build_graph_data(focus_character=character)


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------

@router.get("/")
async def list_relationships(character: Optional[str] = Query(None)):
    """List all relationships, optionally filtered by character."""
    if character:
        return get_character_relationships(character)
    return get_all_relationships()


@router.get("/{char_a}/{char_b}")
async def get_relationship_detail(
    char_a: str,
    char_b: str):
    """Get a specific relationship including history."""
    rel = get_relationship(char_a, char_b)
    if not rel:
        raise HTTPException(404, f"No relationship between {char_a} and {char_b}")
    return rel


# ---------------------------------------------------------------------------
# Manual update
# ---------------------------------------------------------------------------

class RelationshipUpdate(BaseModel):
    type: Optional[str] = None
    strength: Optional[float] = None
    romantic_tension: Optional[float] = None


@router.put("/{char_a}/{char_b}")
async def update_relationship(
    char_a: str,
    char_b: str,
    body: RelationshipUpdate):
    """Manually adjust relationship type, strength, or romantic tension."""
    valid_types = {"friend", "romantic", "rival", "acquaintance", "enemy", "neutral"}
    if body.type and body.type not in valid_types:
        raise HTTPException(400, f"Invalid type. Must be one of: {valid_types}")

    rel = update_relationship_manual(
        char_a, char_b,
        rel_type=body.type,
        strength=body.strength,
        romantic_tension=body.romantic_tension)
    return rel


@router.post("/reclassify-all")
async def reclassify_all(decay_blocked_tension: bool = Query(True)):
    """Re-classify every relationship using current compatibility rules.

    Use after editing romantic_blocked_with / romantic_interests to fix
    stale "romantic" types. If decay_blocked_tension is True (default),
    romantic_tension is also reset to 0 for newly incompatible pairs.
    """
    return reclassify_all_relationships(decay_blocked_tension=decay_blocked_tension)



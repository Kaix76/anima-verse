"""Secret routes - Geheimnisse pro Character verwalten."""
from fastapi import APIRouter, Request, HTTPException
from typing import Dict, Any
from app.core.log import get_logger
from app.models.secrets import (
    list_secrets, get_secret, add_secret, update_secret, delete_secret,
    get_known_secrets_about)

logger = get_logger("secrets")

router = APIRouter(prefix="/secrets", tags=["secrets"])


@router.get("/{character_name}")
def list_secrets_route(
    character_name: str) -> Dict[str, Any]:
    """Listet alle Geheimnisse eines Characters."""
    if not character_name:
        return {"secrets": []}
    secrets = list_secrets(character_name)
    return {"secrets": secrets}


@router.post("/{character_name}")
async def create_secret_route(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Erstellt ein neues Geheimnis."""
    body = await request.json()
    user_id = body.get("user_id", "")
    content = body.get("content", "").strip()

    if not content:
        raise HTTPException(status_code=400, detail="user_id and content required")

    secret = add_secret(
        character_name=character_name,
        content=content,
        category=body.get("category", "personal"),
        severity=int(body.get("severity", 2)),
        related_characters=body.get("related_characters", []),
        related_location=body.get("related_location"),
        consequences_if_revealed=body.get("consequences_if_revealed", ""),
        source=body.get("source", "manual"),
        known_by=body.get("known_by", []))
    return {"ok": True, "secret": secret}


# IMPORTANT: Static paths must come BEFORE dynamic {secret_id} path

@router.post("/{character_name}/generate")
async def generate_secrets_route(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Generiert neue Geheimnisse via LLM basierend auf Character-Kontext."""
    import asyncio

    body = await request.json()
    user_id = body.get("user_id", "")
    count = int(body.get("count", 2))

    from app.core.secret_engine import generate_secrets

    # LLM-Call in Thread ausfuehren um Event Loop nicht zu blockieren
    loop = asyncio.get_event_loop()
    created = await loop.run_in_executor(
        None, lambda: generate_secrets(character_name, count=count)
    )

    if not created:
        raise HTTPException(status_code=500, detail="Generierung fehlgeschlagen — kein LLM verfuegbar oder Fehler bei der Verarbeitung")

    return {"ok": True, "generated": created, "count": len(created)}


@router.get("/{character_name}/known-about-others")
def known_secrets_route(
    character_name: str) -> Dict[str, Any]:
    """Gibt Geheimnisse zurueck die der Character ueber andere kennt."""
    known = get_known_secrets_about(character_name)
    return {"known_secrets": known}


@router.get("/{character_name}/{secret_id}")
def get_secret_route(
    character_name: str,
    secret_id: str) -> Dict[str, Any]:
    """Gibt ein einzelnes Geheimnis zurueck."""
    secret = get_secret(character_name, secret_id)
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"secret": secret}


@router.put("/{character_name}/{secret_id}")
async def update_secret_route(
    character_name: str,
    secret_id: str,
    request: Request) -> Dict[str, Any]:
    """Aktualisiert ein Geheimnis."""
    body = await request.json()
    user_id = body.get("user_id", "")

    updates = {k: v for k, v in body.items() if k != "user_id"}
    updated = update_secret(character_name, secret_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"ok": True, "secret": updated}


@router.delete("/{character_name}/{secret_id}")
def delete_secret_route(
    character_name: str,
    secret_id: str) -> Dict[str, Any]:
    """Loescht ein Geheimnis."""
    deleted = delete_secret(character_name, secret_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"ok": True}

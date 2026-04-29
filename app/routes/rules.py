"""API-Routes fuer das Rules-System (Blockade + Zwangs-Regeln)."""
from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Request

from app.models.rules import load_rules, add_rule, update_rule, delete_rule, get_rule

router = APIRouter(prefix="/rules", tags=["rules"])


# --- Status Modifiers (MUSS vor /{rule_id} stehen!) ---

@router.get("/modifiers")
def get_modifiers_route() -> Dict[str, Any]:
    """Gibt konfigurierte Status-Modifier zurueck."""
    from app.core.danger_system import _load_status_modifiers
    return {"modifiers": _load_status_modifiers()}


@router.put("/modifiers")
async def save_modifiers_route(request: Request) -> Dict[str, Any]:
    """Speichert Status-Modifier."""
    data = await request.json()
    modifiers = data.get("modifiers", [])
    from app.core.danger_system import save_status_modifiers
    save_status_modifiers(modifiers)
    return {"ok": True}


# --- Rules CRUD ---

@router.get("")
def list_rules_route() -> Dict[str, Any]:
    """Listet alle Regeln."""
    return {"rules": load_rules()}


@router.get("/{rule_id}")
def get_rule_route(rule_id: str) -> Dict[str, Any]:
    """Gibt eine einzelne Regel zurueck."""
    rule = get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden")
    return {"rule": rule}


@router.post("")
async def create_rule_route(request: Request) -> Dict[str, Any]:
    """Erstellt eine neue Regel."""
    data = await request.json()
    rule = data.get("rule", {})
    if not rule.get("name") or not rule.get("type"):
        raise HTTPException(status_code=400, detail="name und type sind Pflichtfelder")
    created = add_rule(rule)
    return {"ok": True, "rule": created}


@router.put("/{rule_id}")
async def update_rule_route(rule_id: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert eine Regel."""
    data = await request.json()
    updates = data.get("rule", {})
    updated = update_rule(rule_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden")
    return {"ok": True, "rule": updated}


@router.delete("/{rule_id}")
def delete_rule_route(rule_id: str) -> Dict[str, Any]:
    """Loescht eine Regel."""
    if delete_rule(rule_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Regel nicht gefunden")

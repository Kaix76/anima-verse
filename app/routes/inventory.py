"""Inventory routes - Items, Raum-Items und Character-Inventar verwalten."""
import time
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import FileResponse
from typing import Dict, Any, List
from app.core.log import get_logger
from app.core.paths import get_storage_dir
from app.models.inventory import (
    list_items, get_item, add_item, update_item, delete_item, set_item_image,
    get_room_items, add_item_to_room, remove_item_from_room,
    get_character_inventory, add_to_inventory, remove_from_inventory,
    update_inventory_entry)

logger = get_logger("inventory")

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ============================================================
# ITEM-DEFINITIONEN (Welt-Level)
# ============================================================

@router.get("/items")
def list_items_route(include_shared: int = Query(0, description="Shared-Items mit auflisten (1)")) -> Dict[str, Any]:
    """Listet alle Item-Definitionen."""
    items = list_items()
    if include_shared:
        from app.models.inventory import list_shared_items
        for s in list_shared_items():
            items.append({**s, "_shared": True})
    return {"items": items}


@router.get("/items-shared")
def list_shared_items_route() -> Dict[str, Any]:
    """Listet alle Shared-Library-Items (welt-uebergreifend)."""
    from app.models.inventory import list_shared_items
    return {"items": list_shared_items()}


@router.post("/items/{item_id}/move-to-shared")
async def move_item_to_shared_route(item_id: str, request: Request) -> Dict[str, Any]:
    from app.models.inventory import move_item_to_shared
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    result = move_item_to_shared(item_id)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("reason", "move failed"))
    return result


@router.post("/items/{item_id}/move-to-world")
async def move_item_to_world_route(item_id: str, request: Request) -> Dict[str, Any]:
    from app.models.inventory import move_item_to_world
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    result = move_item_to_world(item_id)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("reason", "move failed"))
    return result


@router.get("/items/{item_id}")
def get_item_route(
    item_id: str) -> Dict[str, Any]:
    """Gibt ein einzelnes Item zurueck."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"item": item}


@router.post("/items")
async def create_item_route(request: Request) -> Dict[str, Any]:
    """Erstellt ein neues Item."""
    body = await request.json()
    user_id = body.get("user_id", "")
    name = body.get("name", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="user_id and name required")

    item = add_item(
        name=name,
        description=body.get("description", ""),
        category=body.get("category", "tool"),
        image_prompt=body.get("image_prompt", ""),
        rarity=body.get("rarity", "common"),
        stackable=body.get("stackable", False),
        max_stack=int(body.get("max_stack", 1)),
        transferable=body.get("transferable", True),
        consumable=body.get("consumable", False),
        reveals_secret=body.get("reveals_secret"),
        properties=body.get("properties"),
        prompt_fragment=body.get("prompt_fragment", ""),
        outfit_piece=body.get("outfit_piece"),
        effects=body.get("effects"))
    return {"ok": True, "item": item}


@router.put("/items/{item_id}")
async def update_item_route(item_id: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert ein Item."""
    body = await request.json()
    user_id = body.get("user_id", "")

    # Altes prompt_fragment festhalten — Variant-Invalidierung nur wenn sich
    # genau dieser Wert aendert (der Variant-Cache haengt am Prompt-Inhalt,
    # nicht an Name/outfit_types/Bild etc.).
    prev = get_item(item_id) or {}
    prev_fragment = (prev.get("prompt_fragment") or "").strip()

    updates = {k: v for k, v in body.items() if k != "user_id"}
    updated = update_item(item_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")

    new_fragment = (updated.get("prompt_fragment") or "").strip()
    if new_fragment != prev_fragment:
        try:
            from app.core.expression_regen import invalidate_variants_for_item
            invalidate_variants_for_item(item_id)
        except Exception as e:
            logger.debug("Variant-Invalidierung fehlgeschlagen: %s", e)
    return {"ok": True, "item": updated}


@router.delete("/items/{item_id}")
def delete_item_route(
    item_id: str) -> Dict[str, Any]:
    """Loescht ein Item."""
    # Variants erst invalidieren waehrend das Item noch im Inventar auffindbar ist
    try:
        from app.core.expression_regen import invalidate_variants_for_item
        invalidate_variants_for_item(item_id)
    except Exception as e:
        logger.debug("Variant-Invalidierung fehlgeschlagen: %s", e)
    deleted = delete_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.get("/items/{item_id}/image")
def get_item_image_route(
    item_id: str):
    """Liefert das Bild eines Items."""
    item = get_item(item_id)
    if not item or not item.get("image"):
        raise HTTPException(status_code=404, detail="Kein Bild vorhanden")
    # Shared-Items haben ihr Bild in shared/items/{id}/ — sonst world-items-Dir
    if item.get("_shared"):
        from app.core.paths import get_shared_dir
        item_dir = get_shared_dir() / "items" / item_id
    else:
        item_dir = get_storage_dir() / "items" / item_id
    path = item_dir / item["image"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bilddatei nicht gefunden")
    suffix = path.suffix.lower()
    media_types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    return FileResponse(
        str(path),
        media_type=media_types.get(suffix, 'image/png'),
        headers={"Cache-Control": "no-cache"})


@router.get("/items/{item_id}/owners")
def get_item_owners_route(
    item_id: str) -> Dict[str, Any]:
    """Listet alle Characters die ein bestimmtes Item im Inventar haben.
    Rueckgabe: {owners: [{character: str, quantity: int, equipped: bool}]}
    """
    from app.models.character import list_available_characters
    from app.models.inventory import _load_inventory
    owners = []
    for char in list_available_characters():
        try:
            inv = _load_inventory(char).get("inventory", []) or []
        except Exception:
            continue
        for entry in inv:
            if entry.get("item_id") != item_id:
                continue
            owners.append({
                "character": char,
                "quantity": int(entry.get("quantity", 1) or 1),
                "equipped": bool(entry.get("equipped", False)),
            })
            break
    owners.sort(key=lambda x: x["character"].lower())
    return {"owners": owners, "item_id": item_id}


def generate_item_image_sync(item_id: str) -> bool:
    """Synchrone Item-Bild-Generierung (fuer Background-Threads).

    Nutzt Default-Workflow + guenstigstes Backend. Gibt True bei Erfolg zurueck.
    """
    item = get_item(item_id)
    if not item:
        return False
    # Prompt-Kette: image_prompt > prompt_fragment > name
    base = (item.get("image_prompt") or "").strip()
    if not base:
        base = (item.get("prompt_fragment") or "").strip()
    if not base:
        base = (item.get("name") or item_id).strip()
    prompt_text = f"{base}, isolated object on neutral background, product photography, sharp focus, realistic"

    from app.core.dependencies import get_skill_manager
    img_skill = None
    for skill in get_skill_manager().skills:
        if getattr(skill, 'SKILL_ID', '') == "image_generation":
            img_skill = skill
            break
    if not img_skill:
        logger.warning("Item-Bild [%s]: ImageGeneration Skill nicht verfuegbar", item_id)
        return False

    active_wf = getattr(img_skill, '_default_workflow', None)
    backend = img_skill._select_backend()
    if not backend:
        logger.warning("Item-Bild [%s]: Kein Backend verfuegbar", item_id)
        return False

    if backend.prompt_prefix:
        prompt_text = f"{backend.prompt_prefix}, {prompt_text}"
    negative = backend.negative_prompt or ""

    from app.core import config as _cfg
    _w = int(_cfg.get("inventory.item_image_width", 256) or 256)
    _h = int(_cfg.get("inventory.item_image_height", 256) or 256)
    params = {"width": _w, "height": _h}
    if backend.api_type == "comfyui" and active_wf:
        if active_wf.workflow_file:
            params["workflow_file"] = active_wf.workflow_file
        _model_key = "unet" if active_wf.has_input_unet else "model"
        if active_wf.model:
            params[_model_key] = active_wf.model
        if active_wf.clip:
            params["clip_name"] = active_wf.clip

    try:
        from app.core.llm_queue import get_llm_queue, Priority as _P
        _is_local = backend.api_type in ("comfyui", "a1111")
        _vram = (active_wf.vram_required_mb if active_wf and active_wf.vram_required_mb else backend.vram_required_mb) if _is_local else 0
        if _is_local:
            images = get_llm_queue().submit_gpu_task(
                task_type="item_image",
                priority=_P.IMAGE_GEN,
                callable_fn=lambda: backend.generate(prompt_text, negative, params),
                agent_name=item.get("name", item_id),
                label=f"Item: {item.get('name', item_id)}",
                vram_required_mb=_vram,
                gpu_type="comfyui")
        else:
            images = backend.generate(prompt_text, negative, params)
    except Exception as e:
        logger.error("Item-Bild [%s] fehlgeschlagen: %s", item_id, e)
        return False

    if images == "NO_NEW_IMAGE":
        logger.warning("Item-Bild [%s]: ComfyUI Cache-Hit — uebersprungen", item_id)
        return False
    if not images:
        return False

    if item.get("_shared"):
        from app.core.paths import get_shared_dir
        item_dir = get_shared_dir() / "items" / item_id
    else:
        item_dir = get_storage_dir() / "items" / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    image_name = f"{int(time.time())}.png"
    image_path = item_dir / image_name
    image_path.write_bytes(images[0])
    try:
        from app.models.character import postprocess_outfit_image
        processed = postprocess_outfit_image(image_path)
        if processed.name != image_name:
            image_name = processed.name
    except Exception:
        pass
    old_image = item.get("image")
    if old_image and old_image != image_name:
        old_path = item_dir / old_image
        if old_path.exists():
            try:
                old_path.unlink()
            except Exception:
                pass
    set_item_image(item_id, image_name)
    logger.info("Item-Bild [%s] generiert: %s", item_id, image_name)
    return True


@router.post("/items/{item_id}/generate-image")
async def generate_item_image_route(item_id: str, request: Request) -> Dict[str, Any]:
    """Generiert ein Bild fuer ein Item — fire-and-forget.

    Gibt sofort 202 zurueck, Generierung laeuft im Background-Thread
    ueber die GPU-Queue. User kann mehrere Items hintereinander anklicken.
    """
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    import threading
    threading.Thread(
        target=generate_item_image_sync,
        args=(item_id,),
        daemon=True,
        name=f"item-img-{item_id[:8]}").start()
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content={"status": "queued", "item_id": item_id,
                 "item_name": item.get("name", item_id)})


# ============================================================
# RAUM-ITEMS
# ============================================================

@router.get("/rooms/{location_id}/{room_id}")
def list_room_items_route(
    location_id: str,
    room_id: str) -> Dict[str, Any]:
    """Items in einem Raum auflisten."""
    room_items = get_room_items(location_id, room_id)
    # Enrichment: Item-Details hinzufuegen
    enriched = []
    for ri in room_items:
        item = get_item(ri.get("item_id", ""))
        entry = {**ri}
        if item:
            entry["item_name"] = item.get("name", "?")
            entry["item_description"] = item.get("description", "")
            entry["item_category"] = item.get("category", "")
            entry["item_image"] = item.get("image")
        enriched.append(entry)
    return {"items": enriched}


@router.post("/rooms/{location_id}/{room_id}")
async def add_room_item_route(
    location_id: str,
    room_id: str,
    request: Request) -> Dict[str, Any]:
    """Platziert ein Item in einem Raum."""
    body = await request.json()
    user_id = body.get("user_id", "")
    item_id = body.get("item_id", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="user_id and item_id required")

    success = add_item_to_room(
        location_id=location_id,
        room_id=room_id,
        item_id=item_id,
        quantity=int(body.get("quantity", 1)),
        hidden=body.get("hidden", False),
        discovery_difficulty=int(body.get("discovery_difficulty", 0)),
        note=body.get("note", ""))
    if not success:
        raise HTTPException(status_code=400, detail="Failed to place item")
    return {"ok": True}


@router.delete("/rooms/{location_id}/{room_id}/{item_id}")
def remove_room_item_route(
    location_id: str,
    room_id: str,
    item_id: str) -> Dict[str, Any]:
    """Entfernt ein Item aus einem Raum."""
    success = remove_item_from_room(location_id, room_id, item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found in room")
    return {"ok": True}


# ============================================================
# CHARACTER-INVENTAR
# ============================================================

@router.get("/characters/{character_name}")
def get_inventory_route(
    character_name: str,
    include_equipped: bool = Query(True, description="Equipped Items mit anzeigen")) -> Dict[str, Any]:
    """Gibt das Inventar eines Characters zurueck.

    include_equipped=False filtert Pieces/Items aus, die der Character
    aktuell angelegt hat (fuer "Was kann ich noch anziehen"-Listen).
    """
    return get_character_inventory(character_name, include_equipped=include_equipped)


@router.post("/characters/{character_name}")
async def add_inventory_item_route(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Fuegt ein Item zum Character-Inventar hinzu."""
    body = await request.json()
    user_id = body.get("user_id", "")
    item_id = body.get("item_id", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="user_id and item_id required")

    success = add_to_inventory(
        character_name=character_name,
        item_id=item_id,
        quantity=int(body.get("quantity", 1)),
        obtained_from=body.get("obtained_from", ""),
        obtained_method=body.get("obtained_method", "manual"))
    if not success:
        raise HTTPException(status_code=400, detail="Failed to add item (inventory full or item not found)")
    return {"ok": True}


@router.put("/characters/{character_name}/{item_id}")
async def update_inventory_item_route(
    character_name: str,
    item_id: str,
    request: Request) -> Dict[str, Any]:
    """Aktualisiert einen Inventar-Eintrag (equipped, notes)."""
    body = await request.json()
    user_id = body.get("user_id", "")

    updates = {k: v for k, v in body.items() if k != "user_id"}
    success = update_inventory_entry(character_name, item_id, updates)
    if not success:
        raise HTTPException(status_code=404, detail="Item not in inventory")
    return {"ok": True}


@router.delete("/characters/{character_name}/{item_id}")
def remove_inventory_item_route(
    character_name: str,
    item_id: str) -> Dict[str, Any]:
    """Entfernt ein Item aus dem Character-Inventar."""
    success = remove_from_inventory(character_name, item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not in inventory")
    return {"ok": True}


@router.post("/characters/{character_name}/{item_id}/use")
async def use_inventory_item_route(
    character_name: str,
    item_id: str,
    request: Request) -> Dict[str, Any]:
    """Verbraucht ein Item aus dem Inventar (qty -1, Eintrag entfernt wenn 0).

    Body: { user_id }
    """
    from app.models.inventory import consume_item, get_item
    body = await request.json()
    user_id = body.get("user_id", "")
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item nicht gefunden")
    if not item.get("consumable"):
        raise HTTPException(status_code=400, detail=f"Item '{item.get('name', item_id)}' ist nicht consumable")
    result = consume_item(character_name, item_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Item nicht im Inventar")
    return {"ok": True, "item_name": item.get("name", item_id),
            "changes": result.get("changes", {}),
            "condition_applied": result.get("condition_applied")}


@router.post("/characters/{character_name}/{item_id}/give")
async def give_inventory_item_route(
    character_name: str,
    item_id: str,
    request: Request) -> Dict[str, Any]:
    """Verschenkt ein Item an einen anderen Character.

    Body: { user_id, to_character }
    Returns: { ok, boost, item_name, rarity }
    """
    from app.models.inventory import gift_item
    body = await request.json()
    user_id = body.get("user_id", "")
    to_character = (body.get("to_character") or "").strip()
    if not to_character:
        raise HTTPException(status_code=400, detail="to_character required")
    if to_character == character_name:
        raise HTTPException(status_code=400, detail="Character kann sich nicht selbst beschenken")
    result = gift_item(character_name, to_character, item_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Verschenken fehlgeschlagen"))
    return {"ok": True, **result}


@router.post("/characters/{character_name}/pickup")
async def pickup_inventory_item_route(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Character hebt ein Item aus einem Raum auf — Raum -> Inventar.

    Body: { user_id, location_id, room_id, item_id, quantity? }
    Returns: { ok, item_name }
    """
    from app.models.inventory import pick_up_item
    body = await request.json()
    location_id = (body.get("location_id") or "").strip()
    room_id = (body.get("room_id") or "").strip()
    item_id = (body.get("item_id") or "").strip()
    quantity = int(body.get("quantity") or 1)
    if not (location_id and room_id and item_id):
        raise HTTPException(status_code=400, detail="location_id, room_id und item_id sind Pflicht")
    result = pick_up_item(character_name, location_id, room_id, item_id, quantity=quantity)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Aufheben fehlgeschlagen"))
    return {"ok": True, **result}


@router.post("/characters/{character_name}/{item_id}/drop")
async def drop_inventory_item_route(
    character_name: str,
    item_id: str,
    request: Request) -> Dict[str, Any]:
    """Character legt ein Item aus dem Inventar in einen Raum ab — Inventar -> Raum.

    Body: { user_id, location_id, room_id, quantity? }
    Returns: { ok, item_name }
    """
    from app.models.inventory import drop_item
    body = await request.json()
    location_id = (body.get("location_id") or "").strip()
    room_id = (body.get("room_id") or "").strip()
    quantity = int(body.get("quantity") or 1)
    if not (location_id and room_id):
        raise HTTPException(status_code=400, detail="location_id und room_id sind Pflicht")
    result = drop_item(character_name, location_id, room_id, item_id, quantity=quantity)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Ablegen fehlgeschlagen"))
    return {"ok": True, **result}


# ============================================================
# EQUIPMENT (Outfit-Pieces + sonstige Ausruestung)
# ============================================================

@router.get("/characters/{character_name}/equipped")
def get_equipped_route(
    character_name: str) -> Dict[str, Any]:
    """Liefert den Equipped-Zustand eines Characters: Pieces (slot->item)
    und sonstige Ausruestung (Liste).
    """
    from app.models.inventory import get_equipped_pieces, get_equipped_items
    from app.models.character import get_character_profile
    _profile = get_character_profile(character_name) or {}
    return {
        "equipped_pieces": get_equipped_pieces(character_name),
        "equipped_items": get_equipped_items(character_name),
        "equipped_pieces_meta": _profile.get("equipped_pieces_meta") or {},
    }


@router.post("/characters/{character_name}/pieces/{slot}/color")
async def set_piece_color_route(character_name: str, slot: str, request: Request) -> Dict[str, Any]:
    """Setzt/loescht die Farbe eines aktuell equippten Pieces.

    Body: {"user_id": "...", "color": "red"}  (leerer color loescht den Eintrag)
    Die Farbe wird pro Character + Slot gespeichert (equipped_pieces_meta).
    """
    from app.models.inventory import set_equipped_piece_color
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    color = (body.get("color") or "").strip()

    result = set_equipped_piece_color(character_name, slot, color)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("reason", "color update failed"))

    # Variant-Cache invalidieren: Prompt aendert sich durch neue Farbe
    item_id = result.get("item_id") or ""
    if item_id:
        try:
            from app.core.expression_regen import invalidate_variants_for_item
            invalidate_variants_for_item(item_id)
        except Exception as e:
            logger.debug("Variant-Invalidierung (color change) fehlgeschlagen: %s", e)
    return result


@router.post("/characters/{character_name}/equip")
async def equip_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Legt ein Outfit-Piece ODER ein anderes Item an.

    Body: {"user_id": "...", "item_id": "..."}.
    Pieces (category=outfit_piece) werden in ihren Slot equipped, alle
    anderen Items in equipped_items.
    """
    from app.models.inventory import (
        get_item, equip_piece, equip_item)
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    item_id = (body.get("item_id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="user_id and item_id required")
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item.get("category") == "outfit_piece":
        result = equip_piece(character_name, item_id)
    else:
        result = equip_item(character_name, item_id)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("reason", "equip failed"))
    return result


@router.post("/characters/{character_name}/unequip")
async def unequip_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Entfernt ein Piece (per slot ODER item_id) oder ein equipped Item.

    Body-Varianten:
      - {"user_id": "...", "slot": "outer"} — Slot leeren
      - {"user_id": "...", "item_id": "..."} — gezielt das Item entfernen
        (sucht erst in equipped_pieces, dann in equipped_items)
    """
    from app.models.inventory import unequip_piece, unequip_item
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    slot = (body.get("slot") or "").strip()
    item_id = (body.get("item_id") or "").strip()
    if not slot and not item_id:
        raise HTTPException(status_code=400, detail="slot or item_id required")
    # erst als Piece (slot oder item_id), dann als equipped Item
    result = unequip_piece(character_name, slot=slot, item_id=item_id)
    if result.get("status") != "ok" and item_id:
        result = unequip_item(character_name, item_id)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("reason", "unequip failed"))
    return result


@router.get("/outfit-types")
def list_outfit_types_route() -> Dict[str, Any]:
    """Sammelt alle bekannten outfit_types.

    Quellen:
    1. shared/config/outfit_rules.json (authoritative Regeln-Config)
    2. Items (outfit_piece.outfit_types)
    3. Locations/Rooms (outfit_type)

    Dient dem UI als Vorschlagsliste. Case-insensitive Dedup — Rules-Schreibweise
    gewinnt.
    """
    from app.models.world import list_locations
    from app.core.outfit_rules import known_outfit_types

    # Rules zuerst (authoritative Schreibweise)
    ordered: List[str] = []
    seen_lower: set = set()
    for t in known_outfit_types():
        if not t or not t.strip():
            continue
        key = t.strip().lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        ordered.append(t.strip())

    def _add(t: str) -> None:
        if not t or not t.strip():
            return
        key = t.strip().lower()
        if key in seen_lower:
            return
        seen_lower.add(key)
        ordered.append(t.strip())

    for it in list_items():
        op = it.get("outfit_piece") or {}
        for t in (op.get("outfit_types") or []):
            _add(t)
    try:
        for loc in list_locations():
            _add(loc.get("outfit_type") or "")
            for r in (loc.get("rooms") or []):
                _add(r.get("outfit_type") or "")
    except Exception:
        pass

    return {"outfit_types": sorted(ordered, key=str.lower)}


@router.post("/characters/{character_name}/apply-equipped")
async def apply_equipped_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Uebertraegt eine Piece-Kombination direkt aufs Profil.

    Body:
        user_id: str
        pieces: {slot: item_id}  — soll-Zustand pro Slot
        remove_slots: [slot, ...] — diese Slots werden explizit geleert

    Slots die belegt sind aber nicht in pieces auftauchen, werden ebenfalls
    geleert (pieces ist der vollstaendige Soll-State).
    """
    from app.models.inventory import apply_equipped_pieces
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    pieces = body.get("pieces") or {}
    remove_slots = body.get("remove_slots") or []
    pieces_meta = body.get("pieces_meta") or {}
    if not isinstance(pieces, dict):
        raise HTTPException(status_code=400, detail="pieces must be {slot: item_id}")
    if not isinstance(pieces_meta, dict):
        pieces_meta = {}
    result = apply_equipped_pieces(character_name,
        pieces=pieces, remove_slots=remove_slots,
        pieces_meta=pieces_meta, source="ui_wardrobe")
    return {
        "status": "ok",
        "changed": result["changed"],
        "applied": result["applied"],
        "skipped": result["skipped"],
        "cleared": result["cleared"],
    }


@router.post("/characters/{character_name}/apply-outfit-set")
async def apply_outfit_set_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Wendet ein gespeichertes Outfit-Set (mit Piece-Liste) an.

    Body: {"user_id": "...", "outfit_id": "..."} ODER {"name": "..."}.

    Equipped jedes Piece aus outfit.pieces[].
    """
    from app.models.character import get_character_outfits
    from app.models.inventory import apply_equipped_pieces, get_item
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    outfit_id = (body.get("outfit_id") or "").strip()
    name = (body.get("name") or "").strip()
    if not user_id or (not outfit_id and not name):
        raise HTTPException(status_code=400, detail="user_id + outfit_id|name required")

    outfits = get_character_outfits(character_name)
    target = None
    for o in outfits:
        if outfit_id and o.get("id") == outfit_id:
            target = o
            break
        if name and (o.get("name") or "").lower() == name.lower():
            target = o
            break
    if not target:
        raise HTTPException(status_code=404, detail="outfit not found")

    # Preset-Pieces (Liste von item_ids) in slot->id mappen.
    pieces_by_slot: Dict[str, str] = {}
    for pid in (target.get("pieces") or []):
        it = get_item(pid) or {}
        slot = ((it.get("outfit_piece") or {}).get("slot") or "").strip()
        if slot:
            pieces_by_slot[slot] = pid

    # Gespeicherte Farben pro Slot in pieces_meta uebersetzen
    saved_colors = target.get("pieces_colors") or {}
    pieces_meta: Dict[str, Dict[str, Any]] = {}
    if isinstance(saved_colors, dict):
        for _slot, _color in saved_colors.items():
            if _color and pieces_by_slot.get(_slot):
                pieces_meta[_slot] = {"color": str(_color).strip()}

    result = apply_equipped_pieces(character_name,
        pieces=pieces_by_slot,
        remove_slots=list(target.get("remove_slots") or []),
        pieces_meta=pieces_meta,
        source="outfit_preset")
    return {
        "status": "ok",
        "outfit_id": target.get("id"),
        "outfit_name": target.get("name"),
        "changed": result["changed"],
        "applied": result["applied"],
        "skipped": result["skipped"],
        "cleared": result["cleared"],
    }

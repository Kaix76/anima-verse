"""World routes - Orte und Aktivitaeten verwalten (User-Level)"""
import asyncio
import os
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import FileResponse
from typing import Dict, Any
from app.core.log import get_logger

logger = get_logger("world")

from app.models.world import (
    list_locations, add_location, delete_location,
    rename_location, resolve_location, get_location_by_id,
    get_entry_room_id,
    update_location_position,
    list_all_activities,
    get_background_path,
    get_background_images, toggle_background_image, remove_background_image,
    get_gallery_dir, list_gallery_images,
    save_gallery_prompt, get_all_gallery_prompts,
    set_gallery_image_room, get_gallery_image_rooms, remove_gallery_image_room,
    set_gallery_image_type, get_gallery_image_types, remove_gallery_image_type,
    set_gallery_image_meta, get_gallery_image_metas,
    get_room_by_id,
    clear_room_prompt_changed, clear_location_prompt_changed)

router = APIRouter(prefix="/world", tags=["world"])


# === Avatar-Movement (Direction-Pad) ===

@router.get("/avatar/neighbors")
def avatar_neighbors_route() -> Dict[str, Any]:
    """Liefert die Nachbar-Locations des Avatars in jede Himmelsrichtung.

    Response: { "north": {id, name} | null, "south": ..., "east": ..., "west": ... }
    Damit kann das Direction-Pad nicht-erreichbare Richtungen ausblenden,
    statt erst auf der 404-Antwort zu reagieren.
    """
    from app.models.account import get_active_character
    from app.models.character import (
        get_character_current_location, get_character_current_room)

    out = {"north": None, "south": None, "east": None, "west": None,
           "current_location_id": "", "current_location_name": "",
           "at_entry_room": True, "entry_room_name": ""}
    avatar = (get_active_character() or "").strip()
    if not avatar:
        return out
    cur_loc_id = (get_character_current_location(avatar) or "").strip()
    if not cur_loc_id:
        return out
    cur = get_location_by_id(cur_loc_id)
    if not cur:
        return out
    out["current_location_id"] = cur.get("id", "") or ""
    out["current_location_name"] = cur.get("name", "") or ""

    # Departure-Gate: Frontend kann die Richtungs-Pfeile ausblenden, wenn der
    # Avatar nicht im Entry-Room steht. Server-seitige Sperre liegt im
    # avatar_step_route.
    cur_entry = get_entry_room_id(cur)
    cur_room = (get_character_current_room(avatar) or "").strip()
    if cur_entry and cur_room and cur_room != cur_entry:
        out["at_entry_room"] = False
    for _r in (cur.get("rooms") or []):
        if isinstance(_r, dict) and _r.get("id") == cur_entry:
            out["entry_room_name"] = _r.get("name", "") or ""
            break

    cx = int(cur.get("grid_x") or 0)
    cy = int(cur.get("grid_y") or 0)
    deltas = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
    targets = {(cx + dx, cy + dy): direction
               for direction, (dx, dy) in deltas.items()}
    for loc in list_locations():
        key = (int(loc.get("grid_x") or 0), int(loc.get("grid_y") or 0))
        direction = targets.get(key)
        if direction and not out[direction]:
            out[direction] = {
                "id": loc.get("id", "") or "",
                "name": loc.get("name", "") or "",
            }
    return out


@router.post("/avatar/step")
async def avatar_step_route(request: Request) -> Dict[str, Any]:
    """Bewegt den Avatar um einen Grid-Schritt in die angegebene Richtung.

    Body: { "direction": "north"|"south"|"east"|"west" }

    Sucht die Nachbar-Location anhand der Grid-Koordinaten der aktuellen
    Avatar-Position. Gibt 404 zurueck wenn dort keine Location liegt.
    """
    data = await request.json()
    direction = (data.get("direction") or "").strip().lower()
    deltas = {
        "north": (0, -1),  # screen-up = decreasing grid_y
        "south": (0, 1),
        "east": (1, 0),
        "west": (-1, 0),
    }
    if direction not in deltas:
        raise HTTPException(status_code=400, detail="invalid direction")

    from app.models.account import get_active_character
    from app.models.character import (
        get_character_current_location,
        get_character_current_room,
        save_character_current_location,
        save_character_current_room,
    )
    avatar = (get_active_character() or "").strip()
    if not avatar:
        raise HTTPException(status_code=400, detail="no active avatar")

    cur_loc_id = (get_character_current_location(avatar) or "").strip()
    if not cur_loc_id:
        raise HTTPException(status_code=400, detail="avatar has no current location")

    cur = get_location_by_id(cur_loc_id)
    if not cur:
        raise HTTPException(status_code=404, detail="current location not found")

    # Departure-Gate: Avatar darf eine Location nur ueber den Entry-Room verlassen.
    cur_entry = get_entry_room_id(cur)
    cur_room = (get_character_current_room(avatar) or "").strip()
    if cur_entry and cur_room and cur_room != cur_entry:
        # Entry-Room-Name zur Meldung holen
        _entry_name = ""
        for _r in (cur.get("rooms") or []):
            if isinstance(_r, dict) and _r.get("id") == cur_entry:
                _entry_name = _r.get("name", "") or ""
                break
        raise HTTPException(status_code=403,
            detail={"reason": "not_at_entry_room",
                    "message": f"Du musst zuerst zum Entry-Room ({_entry_name or cur_entry}) gehen, um diesen Ort zu verlassen."})

    cur_x = int(cur.get("grid_x") or 0)
    cur_y = int(cur.get("grid_y") or 0)
    dx, dy = deltas[direction]
    target_x, target_y = cur_x + dx, cur_y + dy

    # Nachbar-Location suchen
    target = None
    for loc in list_locations():
        if int(loc.get("grid_x") or 0) == target_x and int(loc.get("grid_y") or 0) == target_y:
            target = loc
            break
    if not target:
        raise HTTPException(status_code=404, detail="no location in that direction")

    target_id = target.get("id") or ""

    # Block-Regeln: Avatar unterliegt denselben Restrictions wie NPCs.
    target_entry_room = get_entry_room_id(target)
    from app.models.rules import check_leave, check_access
    ok_leave, leave_msg = check_leave(avatar)
    if not ok_leave:
        raise HTTPException(status_code=403,
            detail={"reason": "block_leave", "message": leave_msg})
    ok_enter, enter_msg = check_access(avatar, target_id, room_id=target_entry_room)
    if not ok_enter:
        raise HTTPException(status_code=403,
            detail={"reason": "block_enter", "message": enter_msg})

    save_character_current_location(avatar, target_id)
    if target_entry_room:
        save_character_current_room(avatar, target_entry_room)

    # Roll-on-Entry: bei Eintritt an einer Location sofort wuerfeln, ob ein
    # Event fuer den Avatar entsteht (z.B. "Wölfe versperren den Weg").
    try:
        from app.core.random_events import try_roll_on_entry
        try_roll_on_entry(avatar, target_id, target)
    except Exception as _re:
        logger.debug("try_roll_on_entry fehlgeschlagen: %s", _re)

    return {
        "ok": True,
        "direction": direction,
        "location_id": target_id,
        "location_name": target.get("name", ""),
        "room_id": target_entry_room,
    }


# === Orte ===

@router.get("/locations")
def get_locations_route(character_name: str = Query("", alias="agent_name")
) -> Dict[str, Any]:
    """Listet Orte aus Sicht eines Characters auf.

    Wenn `character_name` gesetzt ist, werden Orte mit `visible_when`/
    `accessible_when` gegen das Character-Inventar/-State gefiltert. Unsichtbare
    Orte (visible_when schlaegt fehl) werden entfernt; unzugaengliche Orte
    (accessible_when schlaegt fehl) bekommen ein `accessible: false` Flag.
    Ohne `character_name` werden alle Orte ungefiltert zurueckgegeben (Admin-View).
    """
    locations = list_locations()

    if character_name:
        from app.core.activity_engine import evaluate_condition

        def _all_pass(conditions, char: str, loc_id: str) -> bool:
            if not conditions:
                return True
            if isinstance(conditions, str):
                conditions = [conditions]
            for c in conditions:
                if not c:
                    continue
                ok, _ = evaluate_condition(str(c), char, loc_id)
                if not ok:
                    return False
            return True

        filtered = []
        for loc in locations:
            loc_id = loc.get("id", "")
            vw = loc.get("visible_when") or []
            if vw and not _all_pass(vw, character_name, loc_id):
                continue  # Ort nicht sichtbar
            aw = loc.get("accessible_when") or []
            loc["accessible"] = _all_pass(aw, character_name, loc_id) if aw else True
            filtered.append(loc)
        locations = filtered

    for loc in locations:
        loc_id = loc.get("id", "")
        loc["image_count"] = len(list_gallery_images(loc_id)) if loc_id else 0
    return {"locations": locations}


@router.post("/locations")
async def create_location_route(request: Request) -> Dict[str, Any]:
    """Erstellt oder aktualisiert einen Ort."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        location_name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        rooms = data.get("rooms", [])
        image_prompt_day = data.get("image_prompt_day")
        image_prompt_night = data.get("image_prompt_night")
        image_prompt_map = data.get("image_prompt_map")
        danger_level = data.get("danger_level")
        event_settings = data.get("event_settings")
        outfit_type = data.get("outfit_type")
        knowledge_item_id = data.get("knowledge_item_id")
        passable = data.get("passable")
        map_z_offset = data.get("map_z_offset")
        entry_room = data.get("entry_room")
        indoor = data.get("indoor")
        if not location_name:
            raise HTTPException(status_code=400, detail="Name fehlt")
        if not isinstance(rooms, list):
            raise HTTPException(status_code=400, detail="rooms muss eine Liste sein")

        location = add_location(location_name, description, rooms=rooms,
                                image_prompt_day=image_prompt_day,
                                image_prompt_night=image_prompt_night,
                                image_prompt_map=image_prompt_map)

        # Extra-Felder direkt in der Location setzen
        _has_extra = (danger_level is not None or event_settings is not None
                      or outfit_type is not None or knowledge_item_id is not None
                      or passable is not None or map_z_offset is not None
                      or entry_room is not None or indoor is not None)
        if _has_extra and location:
            from app.models.world import _load_world_data, _save_world_data
            wdata = _load_world_data()
            for _l in wdata.get("locations", []):
                if _l.get("id") == location.get("id"):
                    if danger_level is not None:
                        try:
                            _l["danger_level"] = max(0, min(5, int(danger_level)))
                        except (TypeError, ValueError):
                            pass
                    if event_settings is not None:
                        _l["event_settings"] = event_settings
                    if outfit_type is not None:
                        _l["outfit_type"] = (outfit_type or "").strip()
                    if knowledge_item_id is not None:
                        _l["knowledge_item_id"] = (knowledge_item_id or "").strip()
                    if passable is not None:
                        _l["passable"] = bool(passable)
                    if map_z_offset is not None:
                        try:
                            _l["map_z_offset"] = max(-10, min(10, int(map_z_offset)))
                        except (TypeError, ValueError):
                            pass
                    if entry_room is not None:
                        _l["entry_room"] = (entry_room or "").strip()
                    if indoor is not None:
                        _v = (indoor or "").strip().lower()
                        _l["indoor"] = _v if _v in ("indoor", "outdoor") else ""
                    break
            _save_world_data(wdata)
            location = get_location_by_id(location["id"])

        return {"status": "success", "location": location}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/locations/{location_id}")
async def update_location_route(location_id: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert einen Ort (Umbenennung per ID)."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        new_name = data.get("name", "").strip()
        description = data.get("description")
        rooms = data.get("rooms")
        image_prompt_day = data.get("image_prompt_day")
        image_prompt_night = data.get("image_prompt_night")
        image_prompt_map = data.get("image_prompt_map")
        danger_level = data.get("danger_level")
        event_settings = data.get("event_settings")
        outfit_type = data.get("outfit_type")
        knowledge_item_id = data.get("knowledge_item_id")
        passable = data.get("passable")
        map_z_offset = data.get("map_z_offset")
        entry_room = data.get("entry_room")
        indoor = data.get("indoor")

        loc = get_location_by_id(location_id)
        if not loc:
            raise HTTPException(status_code=404, detail="Ort nicht gefunden")

        if new_name:
            rename_location(location_id, new_name)

        # Description, Rooms und Image-Prompts aktualisieren falls mitgegeben
        has_updates = any(v is not None for v in [description, rooms, image_prompt_day, image_prompt_night, image_prompt_map])
        if has_updates:
            loc = get_location_by_id(location_id)
            if loc:
                add_location(loc["name"],
                    description if description is not None else loc.get("description", ""),
                    rooms=rooms if rooms is not None else loc.get("rooms", []),
                    image_prompt_day=image_prompt_day if image_prompt_day is not None else loc.get("image_prompt_day", ""),
                    image_prompt_night=image_prompt_night if image_prompt_night is not None else loc.get("image_prompt_night", ""),
                    image_prompt_map=image_prompt_map if image_prompt_map is not None else loc.get("image_prompt_map", ""))

        # Extra-Felder (inkl. knowledge_item_id) direkt in der Location setzen
        _has_extra = (danger_level is not None or event_settings is not None
                      or outfit_type is not None or knowledge_item_id is not None
                      or passable is not None or map_z_offset is not None
                      or entry_room is not None or indoor is not None)
        if _has_extra:
            from app.models.world import _load_world_data, _save_world_data
            wdata = _load_world_data()
            for _l in wdata.get("locations", []):
                if _l.get("id") == location_id:
                    if danger_level is not None:
                        try:
                            _l["danger_level"] = max(0, min(5, int(danger_level)))
                        except (TypeError, ValueError):
                            pass
                    if event_settings is not None:
                        _l["event_settings"] = event_settings
                    if outfit_type is not None:
                        _l["outfit_type"] = (outfit_type or "").strip()
                    if knowledge_item_id is not None:
                        _l["knowledge_item_id"] = (knowledge_item_id or "").strip()
                    if passable is not None:
                        _l["passable"] = bool(passable)
                    if map_z_offset is not None:
                        try:
                            _l["map_z_offset"] = max(-10, min(10, int(map_z_offset)))
                        except (TypeError, ValueError):
                            pass
                    if entry_room is not None:
                        _l["entry_room"] = (entry_room or "").strip()
                    if indoor is not None:
                        _v = (indoor or "").strip().lower()
                        _l["indoor"] = _v if _v in ("indoor", "outdoor") else ""
                    break
            _save_world_data(wdata)

        updated = get_location_by_id(location_id)
        return {"status": "success", "location": updated}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/locations/{template_id}/clone")
async def clone_location_route(template_id: str, request: Request) -> Dict[str, Any]:
    """Erzeugt eine Klon-Instanz eines (passable) Templates an einer Grid-
    Position. Aufgerufen vom Worldmap-Drag&Drop, wenn der User ein passable
    Template aus dem Tray auf die Karte zieht.
    """
    try:
        data = await request.json()
        grid_x = data.get("grid_x")
        grid_y = data.get("grid_y")
        if grid_x is None or grid_y is None:
            raise HTTPException(status_code=400,
                detail="grid_x/grid_y fehlen")
        from app.models.world import clone_location as _clone
        clone = _clone(template_id, int(grid_x), int(grid_y))
        if not clone:
            raise HTTPException(status_code=404,
                detail="Template nicht gefunden")
        return {"status": "success", "location": clone}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/locations/{location_id}/position")
async def update_location_position_route(location_id: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert die Raster-Position eines Ortes."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        grid_x = data.get("grid_x")
        grid_y = data.get("grid_y")
        if grid_x is None or grid_y is None:
            raise HTTPException(status_code=400, detail="grid_x und grid_y erforderlich")

        loc = update_location_position(location_id, int(grid_x), int(grid_y))
        if not loc:
            raise HTTPException(status_code=404, detail="Ort nicht gefunden")
        return {"status": "success", "location": loc}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/locations/{location_name}")
def delete_location_route(
    location_name: str,
    character_name: str = Query("", alias="agent_name")
) -> Dict[str, Any]:
    """Loescht einen Ort (per ID oder Name)."""
    if delete_location(location_name):
        return {"status": "success", "deleted": location_name}
    raise HTTPException(status_code=404, detail="Ort nicht gefunden")


# === Aktivitaeten (flache Compat-Liste) ===

@router.get("/activities")
def get_activities_route(character_name: str = Query("", alias="agent_name")
) -> Dict[str, Any]:
    """Listet alle Aktivitaeten eines Users auf (flat, dedupliziert)."""
    return {"activities": list_all_activities()}


@router.get("/conditions/list")
def list_conditions() -> Dict[str, Any]:
    """Liste aller Filter-IDs aus prompt_filters (shared + world overlay).

    Die Filter-`id` ist gleichzeitig der kanonische Condition-Name:
    sobald sie als Tag im Profil (active_conditions) steht, triggert der
    zugehoerige Filter implizit. Eine zusaetzliche `condition`-Expression
    am Filter (z.B. ``stamina<10``) wirkt als zweiter Auto-Trigger.

    Returns: {"conditions": [{"name": "drunk", "label": "...", "icon": "🍺"}, ...]}
    """
    from app.core.prompt_filters import load_filters
    seen: Dict[str, Dict[str, Any]] = {}
    for f in load_filters():
        if not f.get("enabled", True):
            continue
        fid = (f.get("id") or "").strip().lower()
        if not fid or fid in seen:
            continue
        seen[fid] = {
            "name": fid,
            "label": (f.get("label") or "").strip(),
            "icon": (f.get("icon") or "").strip(),
        }
    return {"conditions": sorted(seen.values(), key=lambda e: e["name"])}


# === Hintergrundbilder ===

@router.head("/locations/{location_name}/background")
@router.get("/locations/{location_name}/background")
def get_location_background(
    location_name: str,
    room: str = Query("", description="Raum-ID fuer Bild-Filterung"),
    hour: int = Query(-1, description="Aktuelle Stunde (0-23) fuer Tag/Nacht-Auswahl")):
    """Liefert das Hintergrundbild eines Ortes (per ID oder Name)."""
    # location_name kann ID oder Name sein
    bg_path = get_background_path(location_name, room=room, hour=hour)
    if not bg_path or not bg_path.exists():
        raise HTTPException(status_code=404, detail="Kein Hintergrundbild vorhanden")
    suffix = bg_path.suffix.lower()
    media_types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    return FileResponse(
        str(bg_path),
        media_type=media_types.get(suffix, 'image/png'),
        headers={"Cache-Control": "no-cache"}
    )


@router.head("/locations/{location_name}/map-icon")
@router.get("/locations/{location_name}/map-icon")
def get_location_map_icon(
    location_name: str):
    """Liefert das Karten-Icon eines Ortes (erstes als 'map' getaggtes Bild)."""

    loc = resolve_location(location_name)
    if not loc:
        raise HTTPException(status_code=404, detail="Ort nicht gefunden")

    loc_id = loc.get("id", "")
    if not loc_id:
        raise HTTPException(status_code=404, detail="Kein Karten-Bild vorhanden")

    # Klone teilen das Map-Icon ihres Templates — Galerie liegt unter der
    # Template-ID. resolve_location liefert das gemergte Dict, owner_id
    # entscheidet wo das Bildmaterial wirklich liegt.
    from app.models.world import _gallery_owner_id
    owner_id = _gallery_owner_id(location_name) or loc_id

    image_types = get_gallery_image_types(owner_id)
    map_images = [img for img, t in image_types.items() if t == "map"]
    if not map_images:
        raise HTTPException(status_code=404, detail="Kein Karten-Bild vorhanden")

    gallery_dir = get_gallery_dir(owner_id)
    for img_name in map_images:
        img_path = gallery_dir / img_name
        if img_path.exists():
            suffix = img_path.suffix.lower()
            media_types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
            return FileResponse(
                str(img_path),
                media_type=media_types.get(suffix, 'image/png'),
                headers={"Cache-Control": "max-age=300"}
            )

    raise HTTPException(status_code=404, detail="Kein Karten-Bild vorhanden")


@router.post("/locations/{location_name}/background")
async def generate_location_background(location_name: str, request: Request) -> Dict[str, Any]:
    """Generiert ein Hintergrundbild fuer einen Ort per Image-Backend (per ID oder Name)."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        custom_prompt = data.get("prompt", "").strip()

        # Location per ID oder Name aufloesen
        location = resolve_location(location_name)
        if not location:
            raise HTTPException(status_code=404, detail=f"Ort '{location_name}' nicht gefunden")

        description = location.get("description", location_name)

        # Prompt zusammenbauen
        if custom_prompt:
            prompt = custom_prompt
        else:
            prompt = (
                f"{description}, wide angle establishing shot, no people, "
                f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
            )

        # Image-Backend holen (guenstigstes verfuegbares)
        from app.core.dependencies import get_skill_manager

        skill_manager = get_skill_manager()
        img_skill = None
        for skill in skill_manager.skills:
            if getattr(skill, 'SKILL_ID', '') == "image_generation":
                img_skill = skill
                break

        if not img_skill:
            raise HTTPException(status_code=503, detail="ImageGeneration Skill nicht verfuegbar")

        backend = img_skill._select_backend()
        if not backend:
            raise HTTPException(status_code=503, detail="Kein Image-Backend verfuegbar")

        # Bild generieren (blockierend in Thread)
        full_prompt = prompt
        if backend.prompt_prefix:
            full_prompt = f"{backend.prompt_prefix}, {full_prompt}"

        negative = backend.negative_prompt or ""
        # Location-Background: volle Aufloesung — wird als Hintergrund-Szenenbild
        # genutzt, kein Downscale.
        params = {"width": 1280, "height": 720}
        # Default-Workflow und Model ermitteln
        active_wf = getattr(img_skill, '_default_workflow', None)
        if active_wf and active_wf.workflow_file:
            params["workflow_file"] = active_wf.workflow_file
        # Model setzen (input_unet vs input_model)
        if active_wf and active_wf.model:
            _model_key = "unet" if active_wf.has_input_unet else "model"
            params[_model_key] = active_wf.model
            # Model-Verfuegbarkeit pruefen und ggf. aehnlichstes Modell finden
            _resolved = img_skill.resolve_model_for_backend(
                params[_model_key], backend, active_wf.model_type if active_wf else "")
            if _resolved and _resolved != params[_model_key]:
                logger.info("Model-Resolve: %s -> %s (Backend: %s)", params[_model_key], _resolved, backend.name)
                params[_model_key] = _resolved
        # CLIP-Pairing fuer Flux2-Workflows
        if active_wf and active_wf.clip:
            params["clip_name"] = active_wf.clip

        # Backend-Fallback-Engine: probiert primary, faellt bei Fehler auf
        # backend.fallback_mode (none/next_cheaper/specific) zurueck.
        def _op(b):
            return b.generate(full_prompt, negative, params)
        try:
            images, backend = await asyncio.to_thread(
                lambda: img_skill.run_with_fallback(
                    primary_backend=backend,
                    op=_op,
                    workflow=active_wf,
                    character_name=""))
        except RuntimeError as _err:
            raise HTTPException(status_code=500, detail=str(_err))

        # ComfyUI Cache-Hit: Backend gibt String-Sentinel zurueck.
        if images == "NO_NEW_IMAGE":
            raise HTTPException(
                status_code=409,
                detail="ComfyUI hat das Bild bereits mit diesem Seed/Model erzeugt "
                       "(Cache-Hit). Erneut versuchen oder Backend neu starten.")

        if not images:
            raise HTTPException(status_code=500, detail="Bildgenerierung fehlgeschlagen")

        # In Gallery speichern + als Hintergrund referenzieren
        import time
        loc_id = location.get("id", location_name)
        gallery_dir = get_gallery_dir(loc_id)
        gallery_dir.mkdir(parents=True, exist_ok=True)
        image_name = f"{int(time.time())}.png"
        image_path = gallery_dir / image_name
        image_path.write_bytes(images[0])

        # Automatisch als Hintergrund markieren
        toggle_background_image(loc_id, image_name)

        logger.info("Bild generiert + als Hintergrund markiert: %s (%s) -> gallery/%s/%s", location['name'], loc_id, loc_id, image_name)
        return {"status": "success", "location": location["name"], "location_id": loc_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Background Fehler: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/locations/{location_name}/background")
async def delete_location_background(request: Request, location_name: str) -> Dict[str, Any]:
    """Loescht die Hintergrundbild-Referenz eines Ortes (per ID oder Name)."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()

        loc = resolve_location(location_name)
        loc_id = loc["id"] if loc and loc.get("id") else location_name
        # Alle Hintergrund-Markierungen entfernen
        for img in get_background_images(loc_id):
            toggle_background_image(loc_id, img)
        return {"status": "success", "location": location_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Location-Galerie ===

@router.get("/locations/{location_name}/gallery")
def get_location_gallery(
    location_name: str) -> Dict[str, Any]:
    """Listet alle Galerie-Bilder eines Ortes auf (mit Hintergrund-Status)."""
    loc = resolve_location(location_name)
    loc_id = loc["id"] if loc and loc.get("id") else location_name
    images = list_gallery_images(location_name)
    bg_images = get_background_images(loc_id)
    image_rooms = get_gallery_image_rooms(loc_id)
    image_types = get_gallery_image_types(loc_id)
    image_metas = get_gallery_image_metas(loc_id)
    prompts = get_all_gallery_prompts(loc_id)
    # Raeume fuer Dropdown im Frontend
    location_rooms = []
    if loc:
        for room in loc.get("rooms", []):
            location_rooms.append({
                "id": room.get("id", ""),
                "name": room.get("name", ""),
            })
    return {
        "images": images,
        "background_images": bg_images,
        "image_prompts": prompts,
        "image_rooms": image_rooms,
        "image_types": image_types,
        "image_metas": image_metas,
        "location_rooms": location_rooms,
        "location": location_name,
    }


@router.get("/locations/{location_name}/gallery/{image_name}")
def get_gallery_image(
    location_name: str,
    image_name: str):
    """Liefert ein einzelnes Galerie-Bild."""
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")
    gallery_dir = get_gallery_dir(location_name)
    image_path = gallery_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    suffix = image_path.suffix.lower()
    media_types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    return FileResponse(
        str(image_path),
        media_type=media_types.get(suffix, 'image/png'),
        headers={"Cache-Control": "no-cache"}
    )


@router.get("/imagegen-options")
def get_imagegen_options() -> Dict[str, Any]:
    """Gibt verfuegbare Image-Generation-Backends/Workflows zurueck (ohne Character-Bindung)."""
    from app.core.dependencies import get_skill_manager

    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"options": []}

    options = []
    # ComfyUI-Workflows
    for wf in imagegen.get_comfy_workflows():
        options.append({
            "type": "workflow",
            "name": wf["name"],
            "label": f"ComfyUI: {wf['name']}",
            "has_loras": wf.get("has_loras", False),
            "default_loras": wf.get("default_loras", []),
            "model_type": wf.get("model_type", ""),
            "default_model": wf.get("model", ""),
            "filter": wf.get("filter", ""),
        })
    # Nicht-ComfyUI Backends
    for b in imagegen.backends:
        if b.api_type == "comfyui":
            continue
        if not b.available or not b.instance_enabled:
            continue
        opt = {
            "type": "backend",
            "name": b.name,
            "label": b.name,
        }
        # Backend mit Modellliste (z.B. Together.ai) — direkt als Auswahl anbieten
        backend_models = getattr(b, 'available_models', [])
        if backend_models:
            opt["models"] = backend_models
            opt["default_model"] = getattr(b, 'model', backend_models[0])
        options.append(opt)
    # Default-Vorauswahl fuer Location aus .env
    loc_default = os.environ.get("LOCATION_IMAGEGEN_DEFAULT", "").strip()
    result = {"options": options}
    if loc_default:
        result["default_location"] = loc_default
    return result


@router.get("/imagegen-models")
def get_imagegen_models(model_type: str = Query("", description="unet|checkpoint")) -> Dict[str, Any]:
    """Gibt verfuegbare Modelle zurueck (aus Startup-Cache, ohne Character-Bindung)."""
    from app.core.dependencies import get_skill_manager

    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"models": []}

    models = imagegen.get_cached_checkpoints(model_type)
    models_by_service = imagegen.get_cached_checkpoints_by_service(model_type)
    return {"models": models, "models_by_service": models_by_service}


@router.get("/imagegen-loras")
def get_imagegen_loras() -> Dict[str, Any]:
    """Gibt verfuegbare LoRAs zurueck (aus Startup-Cache, ohne Character-Bindung)."""
    from app.core.dependencies import get_skill_manager

    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"loras": []}

    loras = imagegen.get_cached_loras()
    return {"loras": ["None"] + loras}


@router.post("/locations/{location_name}/gallery/batch")
async def generate_gallery_batch(location_name: str, request: Request) -> Dict[str, Any]:
    """Startet Batch-Generierung aller Bilder fuer einen Ort (Background-Task)."""
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    jobs = data.get("jobs", [])
    workflow = data.get("workflow", "").strip()
    backend_name = data.get("backend", "").strip()
    loras = data.get("loras")
    model_override = data.get("model_override", "").strip()
    if not jobs:
        raise HTTPException(status_code=400, detail="Keine Jobs angegeben")

    location = resolve_location(location_name)
    if not location:
        raise HTTPException(status_code=404, detail=f"Ort '{location_name}' nicht gefunden")

    # Alle Jobs vorab als pending Tracked-Tasks registrieren,
    # damit sie im Queue-Panel sichtbar sind
    from app.core.task_queue import get_task_queue
    _tq = get_task_queue()
    _batch_track_ids = []
    for job in jobs:
        _tid = _tq.track_start(
            "image_gen",
            job.get("label", "Ort-Bild"),
            agent_name=location.get("name", location_name),
            start_running=False)
        _batch_track_ids.append(_tid)

    async def _run_batch():
        for i, job in enumerate(jobs):
            _track_id = _batch_track_ids[i]
            try:
                body = {"user_id": "", "_batch_track_id": _track_id}
                if job.get("room_id"):
                    body["room_id"] = job["room_id"]
                if job.get("prompt_type"):
                    body["prompt_type"] = job["prompt_type"]
                if workflow:
                    body["workflow"] = workflow
                if backend_name:
                    body["backend"] = backend_name
                if loras:
                    body["loras"] = loras
                if model_override:
                    body["model_override"] = model_override

                class _MockRequest:
                    async def json(self):
                        return body

                await generate_gallery_image(location_name, _MockRequest())
                logger.info("Batch-Job fertig: %s / %s", location.get("name"), job.get("label", ""))
            except Exception as e:
                _tq.track_finish(_track_id, error=str(e))
                logger.warning("Batch-Job fehlgeschlagen: %s / %s: %s",
                               location.get("name"), job.get("label", ""), e)

    # Background-Task starten
    asyncio.ensure_future(_run_batch())

    return {
        "status": "started",
        "location": location.get("name"),
        "job_count": len(jobs),
    }


@router.post("/locations/{location_name}/gallery")
async def generate_gallery_image(location_name: str, request: Request) -> Dict[str, Any]:
    """Generiert ein neues Galerie-Bild fuer einen Ort (per ID oder Name)."""
    import time

    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        custom_prompt = data.get("prompt", "").strip()
        room_id = data.get("room_id", "").strip()
        prompt_type = data.get("prompt_type", "").strip()  # day/night/map/description
        workflow_name = data.get("workflow", "").strip()
        backend_name = data.get("backend", "").strip()
        loras_override = data.get("loras")
        model_override = data.get("model_override", "").strip()
        batch_track_id = data.get("_batch_track_id", "")

        location = resolve_location(location_name)
        if not location:
            raise HTTPException(status_code=404, detail=f"Ort '{location_name}' nicht gefunden")

        # Prompt-Quelle: custom_prompt > Raum+Typ > Raum > Prompt-Typ > Ortsbeschreibung
        if custom_prompt:
            prompt = custom_prompt
        else:
            description = ""
            if room_id:
                room = get_room_by_id(location, room_id)
                if room:
                    # Raum mit Prompt-Typ: Tag/Nacht-Prompt des Raums bevorzugen
                    if prompt_type == "day":
                        description = (room.get("image_prompt_day", "") or "").strip()
                    elif prompt_type == "night":
                        description = (room.get("image_prompt_night", "") or "").strip()
                    if not description:
                        description = room.get("image_prompt_day", "") or room.get("description", "")
            if not description and prompt_type == "day":
                description = location.get("image_prompt_day", "").strip()
            elif not description and prompt_type == "night":
                description = location.get("image_prompt_night", "").strip()
            elif not description and prompt_type == "map":
                description = location.get("image_prompt_map", "").strip()
            if not description:
                description = location.get("description", location.get("name", location_name))

            if prompt_type == "map":
                prompt = (
                    f"{description}, small icon, top-down view, miniature, "
                    f"game map tile, simple, clean, centered"
                )
            else:
                prompt = (
                    f"{description}, wide angle establishing shot, no people, "
                    f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                )

        from app.core.dependencies import get_skill_manager

        skill_manager = get_skill_manager()
        img_skill = None
        for skill in skill_manager.skills:
            if getattr(skill, 'SKILL_ID', '') == "image_generation":
                img_skill = skill
                break

        if not img_skill:
            raise HTTPException(status_code=503, detail="ImageGeneration Skill nicht verfuegbar")

        # Verfuegbarkeit aller Backends frisch pruefen
        for b in img_skill.backends:
            if b.instance_enabled:
                b.check_availability()

        # Backend-Auswahl: explizit > Workflow > Auto (guenstigster)
        backend = None
        if workflow_name:
            wf = next((w for w in img_skill.comfy_workflows if w.name == workflow_name), None)
            if wf:
                # Kompatiblen Backend fuer Workflow finden (nur ComfyUI, da Workflow)
                compat = wf.compatible_backends or []
                logger.info(
                    "Workflow '%s': compatible_backends=%s, alle Backends: [%s]",
                    workflow_name, compat or "alle",
                    ", ".join(
                        f"{b.name}(type={b.api_type}, available={b.available}, "
                        f"enabled={b.instance_enabled})"
                        for b in img_skill.backends
                    ))
                candidates = [b for b in img_skill.backends if b.available and b.instance_enabled
                              and b.api_type == "comfyui"
                              and (not compat or b.name in compat)]
                backend = img_skill.pick_lowest_cost(
                    candidates, rotation_key=f"world_image:{workflow_name}")
                if not backend:
                    # Detaillierte Diagnose welche Bedingung fehlschlaegt
                    for b in img_skill.backends:
                        if b.api_type == "comfyui":
                            reasons = []
                            if not b.available:
                                reasons.append("not available")
                            if not b.instance_enabled:
                                reasons.append("not enabled")
                            if compat and b.name not in compat:
                                reasons.append(f"name '{b.name}' not in {compat}")
                            logger.warning(
                                "Workflow '%s': Backend '%s' ausgeschlossen: %s",
                                workflow_name, b.name,
                                ", ".join(reasons) if reasons else "unbekannt")
                    raise HTTPException(
                        status_code=503,
                        detail=f"Kein ComfyUI-Backend fuer Workflow '{workflow_name}' verfuegbar"
                    )
                logger.info("Expliziter Workflow: %s -> Backend: %s", workflow_name, backend.name)
            else:
                logger.warning(
                    "Workflow '%s' nicht gefunden, verfuegbar: [%s]",
                    workflow_name,
                    ", ".join(wf.name for wf in img_skill.comfy_workflows))
        elif backend_name:
            backend = next((b for b in img_skill.backends if b.name == backend_name and b.available), None)
            logger.debug("Explizites Backend: %s -> %s", backend_name, backend.name if backend else 'nicht verfuegbar')

        if not backend:
            backend = img_skill._select_backend()
        if not backend:
            raise HTTPException(status_code=503, detail="Kein Image-Backend verfuegbar")

        full_prompt = prompt
        if backend.prompt_prefix:
            full_prompt = f"{backend.prompt_prefix}, {full_prompt}"

        negative = backend.negative_prompt or ""
        # Map-Icons sind kleine Thumbnails fuer die Welt-Uebersicht und werden
        # runtergerechnet. Day/Night/Description bleiben in voller Aufloesung
        # als Hintergrund-Bilder.
        params: Dict[str, Any] = {"width": 1280, "height": 720}
        if prompt_type == "map":
            params["image_use_case"] = "map"
        # Workflow-File: expliziter Workflow hat Vorrang vor Default-Workflow
        active_wf = None
        if workflow_name:
            active_wf = next((w for w in img_skill.comfy_workflows if w.name == workflow_name), None)
            if active_wf and active_wf.workflow_file:
                params["workflow_file"] = active_wf.workflow_file
        else:
            _default_wf = getattr(img_skill, '_default_workflow', None)
            if _default_wf and _default_wf.workflow_file:
                active_wf = _default_wf
                params["workflow_file"] = _default_wf.workflow_file
        # Model-Override (input_unet Workflows brauchen "unet" statt "model")
        _model_key = "unet" if (active_wf and active_wf.has_input_unet) else "model"
        if model_override:
            params[_model_key] = model_override
        elif active_wf and active_wf.model:
            params[_model_key] = active_wf.model
        # Model-Verfuegbarkeit pruefen und ggf. aehnlichstes Modell finden
        _current_model = params.get(_model_key, "")
        if _current_model and backend.api_type == "comfyui" and img_skill:
            _resolved = img_skill.resolve_model_for_backend(
                _current_model, backend, active_wf.model_type if active_wf else "")
            if _resolved and _resolved != _current_model:
                logger.info("Model-Resolve: %s -> %s (Backend: %s)", _current_model, _resolved, backend.name)
                params[_model_key] = _resolved
        # CLIP-Pairing fuer Flux2-Workflows
        if active_wf and active_wf.clip:
            params["clip_name"] = active_wf.clip
        # LoRA-Inputs
        if active_wf and active_wf.has_loras:
            if loras_override is not None:
                params["lora_inputs"] = loras_override
            elif active_wf.default_loras:
                params["lora_inputs"] = active_wf.default_loras

        from app.core.task_queue import get_task_queue
        _tq = get_task_queue()
        if batch_track_id:
            # Batch-Modus: vorregistrierten pending Track aktivieren
            _track_id = batch_track_id
            from app.core.task_router import match_queue_name
            _queue = match_queue_name(backend.name) or "default"
            _tq.track_activate(_track_id, queue_name=_queue, provider=backend.name)
        else:
            _track_id = _tq.track_start(
                "image_gen", "Ort-Bild", agent_name=location.get("name", location_name),
                provider=backend.name)

        _gen_start = time.time()
        try:
            # Backend-Fallback-Engine: bei Fehler auf backend.fallback_mode wechseln
            def _op(b):
                return b.generate(full_prompt, negative, params)
            try:
                images, backend = await asyncio.to_thread(
                    lambda: img_skill.run_with_fallback(
                        primary_backend=backend, op=_op,
                        workflow=active_wf, character_name=""))
            except RuntimeError as _err:
                _tq.track_finish(_track_id, error=str(_err)[:200])
                raise HTTPException(status_code=500, detail=str(_err))

            # ComfyUI Cache-Hit: Backend gibt String-Sentinel zurueck. Ohne diese
            # Pruefung wuerde write_bytes(images[0]) ein einzelnes Char schreiben
            # und einen memoryview-TypeError werfen.
            if images == "NO_NEW_IMAGE":
                _tq.track_finish(_track_id, error="Duplikat")
                raise HTTPException(
                    status_code=409,
                    detail="ComfyUI hat das Bild bereits mit diesem Seed/Model erzeugt "
                           "(Cache-Hit). Erneut versuchen oder Backend neu starten.")

            if not images:
                _tq.track_finish(_track_id, error="Bildgenerierung fehlgeschlagen")
                raise HTTPException(status_code=500, detail="Bildgenerierung fehlgeschlagen")

            loc_id = location.get("id", location_name)
            gallery_dir = get_gallery_dir(loc_id)
            gallery_dir.mkdir(parents=True, exist_ok=True)
            image_name = f"{int(time.time())}.png"
            image_path = gallery_dir / image_name
            image_path.write_bytes(images[0])

            # Prompt speichern fuer spaeteres Upgrade
            save_gallery_prompt(loc_id, image_name, full_prompt)

            # Neues Bild standardmaessig als Hintergrund markieren
            toggle_background_image(loc_id, image_name)

            # Raum-Zuordnung setzen wenn room_id angegeben
            if room_id:
                set_gallery_image_room(loc_id, image_name, room_id)
                # prompt_changed Flag entfernen — Bild wurde aus dem Prompt erzeugt
                from app.models.world import clear_room_prompt_changed
                clear_room_prompt_changed(loc_id, room_id)
            elif not custom_prompt:
                # Location-Level Prompt verwendet — Flag dort entfernen
                from app.models.world import clear_location_prompt_changed
                clear_location_prompt_changed(loc_id)

            # Erzeugungs-Metadaten speichern (Service + Model)
            _model_used = (getattr(backend, 'last_used_checkpoint', '')
                           or getattr(backend, 'model', '')
                           or getattr(backend, 'checkpoint', '') or '')
            set_gallery_image_meta(loc_id, image_name, {
                "backend": backend.name,
                "backend_type": backend.api_type,
                "model": _model_used,
            })

            # Bild-Typ setzen wenn prompt_type angegeben (day/night/map)
            if prompt_type in ("day", "night", "map"):
                set_gallery_image_type(loc_id, image_name, prompt_type)

            # Karten-Bilder: Hintergrund entfernen (transparent)
            if prompt_type == "map":
                try:
                    import asyncio as _asyncio
                    from app.models.character import postprocess_outfit_image
                    # rembg/ONNX-Inferenz ist CPU-bound → im Threadpool, sonst
                    # blockiert der Event-Loop ~1s pro Map-Bild.
                    processed = await _asyncio.to_thread(
                        postprocess_outfit_image, image_path)
                    if processed != image_path:
                        # Dateiname hat sich geaendert (.png)
                        new_name = processed.name
                        if new_name != image_name:
                            # Metadaten unter neuem Namen uebertragen
                            save_gallery_prompt(loc_id, new_name, full_prompt)
                            set_gallery_image_type(loc_id, new_name, "map")
                            toggle_background_image(loc_id, new_name)
                            # Alte Metadaten entfernen
                            remove_background_image(loc_id, image_name)
                            remove_gallery_image_type(loc_id, image_name)
                            image_name = new_name
                    logger.info("Karten-Bild Hintergrund entfernt: %s", image_name)
                except Exception as _rembg_err:
                    logger.warning("Hintergrundentfernung fuer Karten-Bild fehlgeschlagen: %s", _rembg_err)

            _tq.track_finish(_track_id)
            _gen_duration = time.time() - _gen_start
            logger.info("Bild generiert: %s (%s)/%s%s", location['name'], loc_id, image_name,
                        f" room={room_id}" if room_id else "")

            # Image-Prompt in JSONL loggen
            try:
                from app.utils.image_prompt_logger import log_image_prompt
                _model_name = (getattr(backend, 'last_used_checkpoint', '')
                               or getattr(backend, 'model', '')
                               or getattr(backend, 'checkpoint', '') or '')
                log_image_prompt(
                    agent_name=location.get("name", location_name),
                    original_prompt=prompt,
                    final_prompt=full_prompt,
                    negative_prompt=negative,
                    backend_name=backend.name,
                    backend_type=backend.api_type,
                    model=_model_name,
                    auto_enhance=False,
                    duration_s=_gen_duration,
                    loras=params.get("lora_inputs", []),
                    reference_images=params.get("reference_images", {}))
            except Exception as _log_err:
                logger.error("Image-Logging Fehler: %s", _log_err)
            return {"status": "success", "location": location["name"], "location_id": loc_id, "image": image_name}
        except HTTPException:
            raise
        except Exception as e:
            _tq.track_finish(_track_id, error=str(e))
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Gallery Fehler: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/locations/{location_name}/gallery/{image_name}")
async def delete_gallery_image(
    location_name: str,
    image_name: str) -> Dict[str, Any]:
    """Loescht ein Galerie-Bild (per ID oder Name)."""
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    loc = resolve_location(location_name)
    loc_id = loc["id"] if loc and loc.get("id") else location_name

    gallery_dir = get_gallery_dir(loc_id)
    image_path = gallery_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    image_path.unlink()

    # Falls das Bild als Hintergrund markiert war, Markierung entfernen
    remove_background_image(loc_id, image_name)
    remove_gallery_image_room(loc_id, image_name)
    remove_gallery_image_type(loc_id, image_name)

    return {"status": "success", "deleted": image_name}


@router.post("/locations/{location_name}/gallery/{image_name}/toggle-background")
async def toggle_gallery_background(
    location_name: str,
    image_name: str,
    request: Request) -> Dict[str, Any]:
    """Toggled ob ein Galerie-Bild als Hintergrund in Frage kommt."""
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    loc = resolve_location(location_name)
    loc_id = loc["id"] if loc and loc.get("id") else location_name

    gallery_dir = get_gallery_dir(loc_id)
    image_path = gallery_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    is_eligible = toggle_background_image(loc_id, image_name)

    return {"status": "success", "image": image_name, "is_background": is_eligible}


@router.post("/locations/{location_name}/gallery/{image_name}/room")
async def set_gallery_image_room_route(
    location_name: str,
    image_name: str,
    request: Request) -> Dict[str, Any]:
    """Setzt den Raum eines Galerie-Bildes."""
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    room_id = body.get("room", "").strip()
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    loc = resolve_location(location_name)
    loc_id = loc["id"] if loc and loc.get("id") else location_name

    gallery_dir = get_gallery_dir(loc_id)
    image_path = gallery_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    set_gallery_image_room(loc_id, image_name, room_id)
    return {"status": "success", "image": image_name, "room": room_id}


@router.post("/locations/{location_name}/gallery/{image_name}/type")
async def set_gallery_image_type_route(
    location_name: str,
    image_name: str,
    request: Request) -> Dict[str, Any]:
    """Setzt den Typ eines Galerie-Bildes (day/night/map oder leer)."""
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    image_type = body.get("type", "").strip()
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")
    if image_type and image_type not in ("day", "night", "map"):
        raise HTTPException(status_code=400, detail="Typ muss 'day', 'night', 'map' oder leer sein")

    loc = resolve_location(location_name)
    loc_id = loc["id"] if loc and loc.get("id") else location_name

    gallery_dir = get_gallery_dir(loc_id)
    image_path = gallery_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    set_gallery_image_type(loc_id, image_name, image_type)
    return {"status": "success", "image": image_name, "type": image_type}


@router.post("/locations/{location_name}/gallery/{image_name}/time-variant")
async def generate_time_variant(
    location_name: str,
    image_name: str,
    request: Request) -> Dict[str, Any]:
    """Erzeugt eine Tag- oder Nachtansicht aus einem bestehenden Bild per img2img (Referenzbild).

    Nutzt einen Qwen-kompatiblen Workflow mit dem Originalbild als Referenz.
    Body-Parameter 'target_type': 'night' (default) oder 'day'.
    """
    import time

    try:
        body = await request.json()
        user_id = body.get("user_id", "").strip()
        target_type = body.get("target_type", "night").strip()
        workflow_name = body.get("workflow", "").strip()
        backend_name = body.get("backend", "").strip()
        custom_prompt = body.get("prompt", "").strip()
        if target_type not in ("day", "night"):
            raise HTTPException(status_code=400, detail="target_type muss 'day' oder 'night' sein")
        if ".." in image_name or "/" in image_name:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        location = resolve_location(location_name)
        if not location:
            raise HTTPException(status_code=404, detail=f"Ort '{location_name}' nicht gefunden")

        loc_id = location.get("id", location_name)
        gallery_dir = get_gallery_dir(loc_id)
        source_path = gallery_dir / image_name
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Quellbild nicht gefunden")

        # Prompt: custom oder automatisch aus Tag/Nacht-Prompt / Beschreibung
        prompt_field = f"image_prompt_{target_type}"
        if custom_prompt:
            prompt = custom_prompt
        else:
            # Raum-Zuordnung des Quellbilds pruefen
            image_rooms = get_gallery_image_rooms(loc_id)
            source_room_id = image_rooms.get(image_name, "")
            description = ""
            is_room = False
            if source_room_id:
                room = get_room_by_id(location, source_room_id)
                if room:
                    is_room = True
                    description = (room.get(prompt_field, "") or
                                   room.get("description", ""))
            if not description:
                description = (location.get(prompt_field, "") or
                               location.get("description", location.get("name", location_name)))

            if is_room:
                # Innenraum: kein Himmel/Sterne, stattdessen Beleuchtung anpassen
                if target_type == "night":
                    prompt = (
                        f"{description}, nighttime interior, dim warm lighting, "
                        f"lamp light, evening atmosphere, cozy shadows, "
                        f"window showing dark sky outside, "
                        f"wide angle interior shot, no people, "
                        f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                    )
                else:
                    prompt = (
                        f"{description}, daytime interior, bright natural light, "
                        f"sunlight through windows, warm daylight atmosphere, "
                        f"wide angle interior shot, no people, "
                        f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                    )
            else:
                # Aussenbereich / Ort
                if target_type == "night":
                    prompt = (
                        f"{description}, nighttime, dark sky, moonlight, "
                        f"night atmosphere, dim lighting, stars, evening mood, "
                        f"wide angle establishing shot, no people, "
                        f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                    )
                else:
                    prompt = (
                        f"{description}, daytime, bright sunlight, clear sky, "
                        f"warm daylight atmosphere, natural lighting, "
                        f"wide angle establishing shot, no people, "
                        f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                    )

        from app.core.dependencies import get_skill_manager

        skill_manager = get_skill_manager()
        img_skill = None
        for skill in skill_manager.skills:
            if getattr(skill, 'SKILL_ID', '') == "image_generation":
                img_skill = skill
                break

        if not img_skill:
            raise HTTPException(status_code=503, detail="ImageGeneration Skill nicht verfuegbar")

        # Verfuegbarkeit pruefen
        for b in img_skill.backends:
            if b.instance_enabled:
                b.check_availability()

        # Backend-Auswahl: explizit > Workflow > Qwen-faehig > Auto
        backend = None
        active_wf = None
        if workflow_name:
            active_wf = next((w for w in img_skill.comfy_workflows if w.name == workflow_name), None)
            if active_wf:
                compat = active_wf.compatible_backends or []
                candidates = [b for b in img_skill.backends if b.available and b.instance_enabled
                              and b.api_type == "comfyui"
                              and (not compat or b.name in compat)]
                backend = img_skill.pick_lowest_cost(
                    candidates, rotation_key=f"time_variant:{active_wf.name}")
        elif backend_name:
            backend = next((b for b in img_skill.backends if b.name == backend_name and b.available), None)

        if not backend:
            # Qwen-Workflow bevorzugen (unterstuetzt Referenzbilder)
            for wf in img_skill.comfy_workflows:
                if "qwen" in wf.name.lower():
                    compat = wf.compatible_backends or []
                    candidates = [b for b in img_skill.backends if b.available and b.instance_enabled
                                  and b.api_type == "comfyui"
                                  and (not compat or b.name in compat)]
                    _picked = img_skill.pick_lowest_cost(
                        candidates, rotation_key=f"time_variant:{wf.name}")
                    if _picked:
                        backend = _picked
                        active_wf = wf
                        break

        # Kein Fallback auf Nicht-ComfyUI Backends — Time-Variant-Convert braucht
        # zwingend ein Backend mit Reference-Image-Support (Qwen via ComfyUI).
        # Cloud-Backends wie Together-Flux koennen kein img2img mit lokalem
        # Reference-Bild + ComfyUI-Workflow.
        if not backend:
            raise HTTPException(
                status_code=503,
                detail="Kein ComfyUI-Backend mit Referenzbild-Support verfuegbar "
                       "(z.B. Qwen-Workflow). Bitte ComfyUI-Backend starten.")

        full_prompt = prompt
        if backend.prompt_prefix:
            full_prompt = f"{backend.prompt_prefix}, {full_prompt}"

        negative = backend.negative_prompt or ""
        # Day/Night-Variants sind Hintergrund-Bilder — voll, kein Downscale.
        params = {"width": 1280, "height": 720}

        # Workflow-Datei setzen
        if active_wf and active_wf.workflow_file:
            params["workflow_file"] = active_wf.workflow_file
        elif not workflow_name:
            _default_wf = getattr(img_skill, '_default_workflow', None)
            if _default_wf and _default_wf.workflow_file:
                active_wf = _default_wf
                params["workflow_file"] = _default_wf.workflow_file

        # Model (input_unet Workflows brauchen "unet" statt "model")
        _model_key = "unet" if (active_wf and active_wf.has_input_unet) else "model"
        if active_wf and active_wf.model:
            params[_model_key] = active_wf.model
        # Model-Verfuegbarkeit pruefen und ggf. aehnlichstes Modell finden
        _current_model = params.get(_model_key, "")
        if _current_model and backend.api_type == "comfyui" and img_skill:
            _resolved = img_skill.resolve_model_for_backend(
                _current_model, backend, active_wf.model_type if active_wf else "")
            if _resolved and _resolved != _current_model:
                logger.info("Model-Resolve: %s -> %s (Backend: %s)", _current_model, _resolved, backend.name)
                params[_model_key] = _resolved

        # CLIP — sonst scheitert ComfyUI mit value_not_in_list bei input_clip
        if active_wf and active_wf.clip:
            params["clip_name"] = active_wf.clip

        # LoRAs
        if active_wf and active_wf.has_loras and active_wf.default_loras:
            params["lora_inputs"] = active_wf.default_loras

        # Frischer Seed pro Aufruf — Time-Variant soll immer ein neues Bild
        # erzeugen, sonst cached ComfyUI bei identischem Prompt+Seed.
        if active_wf and active_wf.has_seed:
            import random as _rnd
            params["seed"] = _rnd.randint(1, 2**31 - 1)

        # Das Source-Bild ist das zu editierende Bild (primaere Edit-Referenz).
        # Qwen erwartet primary Edit-Referenzen auf image1 (Node
        # input_reference_image_1). Slot 4 ist laut Spec fuer separate
        # Location-Referenzen — hier ist das Bild selbst die Location.
        params["reference_images"] = {
            "input_reference_image_1": str(source_path),
        }
        params["string_inputs"] = {
            "input_reference_image_1_type": "location",
        }

        from app.core.task_queue import get_task_queue
        _tq = get_task_queue()
        _variant_label = "Nachtansicht" if target_type == "night" else "Tagansicht"
        _track_id = _tq.track_start(
            "image_gen", _variant_label, agent_name=location.get("name", location_name),
            provider=backend.name)

        _gen_start = time.time()
        try:
            def _op(b):
                return b.generate(full_prompt, negative, params)
            try:
                images, backend = await asyncio.to_thread(
                    lambda: img_skill.run_with_fallback(
                        primary_backend=backend, op=_op,
                        workflow=active_wf, character_name=""))
            except RuntimeError as _err:
                _tq.track_finish(_track_id, error=str(_err)[:200])
                raise HTTPException(status_code=500, detail=str(_err))

            # ComfyUI Cache-Hit: Backend gibt String-Sentinel zurueck. Ohne diese
            # Pruefung wuerde write_bytes(images[0]) ein einzelnes Char schreiben
            # und einen memoryview-TypeError werfen.
            if images == "NO_NEW_IMAGE":
                _tq.track_finish(_track_id, error="Duplikat")
                raise HTTPException(
                    status_code=409,
                    detail="ComfyUI hat das Bild bereits mit diesem Seed/Model erzeugt "
                           "(Cache-Hit). Erneut versuchen oder Backend neu starten.")

            if not images:
                _tq.track_finish(_track_id, error="Bildgenerierung fehlgeschlagen")
                raise HTTPException(status_code=500, detail="Bildgenerierung fehlgeschlagen")

            gallery_dir.mkdir(parents=True, exist_ok=True)
            new_image_name = f"{int(time.time())}.png"
            new_image_path = gallery_dir / new_image_name
            new_image_path.write_bytes(images[0])

            # Prompt speichern
            save_gallery_prompt(loc_id, new_image_name, full_prompt)

            # Als Hintergrund markieren
            toggle_background_image(loc_id, new_image_name)

            # Typ setzen (day/night)
            set_gallery_image_type(loc_id, new_image_name, target_type)

            # Raum-Zuordnung vom Quellbild uebernehmen
            image_rooms = get_gallery_image_rooms(loc_id)
            source_room = image_rooms.get(image_name, "")
            if source_room:
                set_gallery_image_room(loc_id, new_image_name, source_room)

            # Meta speichern
            _model_used = (getattr(backend, 'last_used_checkpoint', '')
                           or getattr(backend, 'model', '')
                           or getattr(backend, 'checkpoint', '') or '')
            set_gallery_image_meta(loc_id, new_image_name, {
                "backend": backend.name,
                "backend_type": backend.api_type,
                "model": _model_used,
                "source": image_name,
            })

            _tq.track_finish(_track_id)
            _gen_duration = time.time() - _gen_start
            logger.info("%s generiert: %s (%s)/%s -> %s", _variant_label, location['name'], loc_id, image_name, new_image_name)

            try:
                from app.utils.image_prompt_logger import log_image_prompt
                log_image_prompt(
                    agent_name=location.get("name", location_name),
                    original_prompt=prompt,
                    final_prompt=full_prompt,
                    negative_prompt=negative,
                    backend_name=backend.name,
                    backend_type=backend.api_type,
                    model=_model_used,
                    auto_enhance=False,
                    duration_s=_gen_duration,
                    loras=params.get("lora_inputs", []),
                    reference_images=params.get("reference_images", {}))
            except Exception as _log_err:
                logger.error("Image-Logging Fehler: %s", _log_err)

            return {"status": "success", "location_id": loc_id, "image": new_image_name, "source": image_name}
        except HTTPException:
            raise
        except Exception as e:
            _tq.track_finish(_track_id, error=str(e))
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Time-Variant Fehler: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/locations/{location_id}/prompt-changed")
async def set_prompt_changed_flag(
    location_id: str,
    request: Request) -> Dict[str, Any]:
    """Setzt oder entfernt das prompt_changed Flag fuer eine Location oder einen Raum.

    Body: {"user_id": "...", "room_id": "..." (optional), "value": true/false}
    Ohne room_id wird das Flag auf Location-Ebene gesetzt/entfernt.
    """
    from app.models.world import _load_world_data, _save_world_data

    try:
        body = await request.json()
        user_id = body.get("user_id", "").strip()
        room_id = body.get("room_id", "").strip()
        value = body.get("value", False)

        if not value:
            # Flag entfernen
            if room_id:
                ok = clear_room_prompt_changed(location_id, room_id)
            else:
                ok = clear_location_prompt_changed(location_id)
            if not ok:
                raise HTTPException(status_code=404, detail="Location/Raum nicht gefunden")
            return {"status": "success", "prompt_changed": False}
        else:
            # Flag setzen
            data = _load_world_data()
            for loc in data.get("locations", []):
                if loc.get("id") == location_id:
                    if room_id:
                        for room in loc.get("rooms", []):
                            if room.get("id") == room_id:
                                room["prompt_changed"] = True
                                _save_world_data(data)
                                return {"status": "success", "prompt_changed": True}
                        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
                    else:
                        loc["prompt_changed"] = True
                        _save_world_data(data)
                        return {"status": "success", "prompt_changed": True}
            raise HTTPException(status_code=404, detail="Location nicht gefunden")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("prompt-changed Fehler: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# === Messaging-Frame (Phone-Chat-Layout) ===

@router.get("/messaging-frame")
async def get_messaging_frame() -> Dict[str, Any]:
    """Liefert Frame-Status + bbox-Metadaten fuer Frontend-Composite.

    Returns:
        {has_frame, path, bbox, frame_size, prompt, backend, generated_at}
        oder {has_frame: False} wenn noch nicht generiert.
    """
    from app.core.messaging_frame import has_frame, load_frame_meta
    if not has_frame():
        return {"has_frame": False}
    meta = load_frame_meta() or {}
    return {
        "has_frame": True,
        "url": "/world/messaging-frame.png",
        **meta,
    }


@router.get("/messaging-frame.png")
async def get_messaging_frame_image() -> FileResponse:
    """Liefert das prozessierte Frame-Bild (PNG mit transparenter Anzeigeflaeche)."""
    from app.core.messaging_frame import get_frame_path, has_frame
    if not has_frame():
        raise HTTPException(status_code=404, detail="Frame nicht generiert")
    return FileResponse(str(get_frame_path()), media_type="image/png")


@router.post("/messaging-frame/generate")
async def post_messaging_frame_generate(request: Request) -> Dict[str, Any]:
    """Generiert das Messaging-Frame neu via image_skill.

    Body: {"prompt": "...", "backend": "Together-Fast" (optional)}

    Pipeline: image_skill.generate -> rembg (aussen) -> Chroma-Key (gruen) -> bbox.
    Laeuft synchron im Worker-Thread (kann 30-90s dauern je nach Backend).
    """
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    target = (body.get("target") or "").strip()  # "workflow:Name" oder "backend:Name"
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt fehlt")
    from app.core.messaging_frame import generate_frame
    result = await asyncio.to_thread(generate_frame, prompt, target)
    if result.get("status") != "ok":
        raise HTTPException(status_code=500, detail=result.get("error", "Generierung fehlgeschlagen"))
    return result


@router.delete("/messaging-frame")
async def delete_messaging_frame() -> Dict[str, Any]:
    """Loescht das aktuelle Frame (Frontend faellt auf CSS-Default zurueck)."""
    from app.core.messaging_frame import get_frame_path, get_frame_meta_path
    for p in (get_frame_path(), get_frame_meta_path()):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    return {"status": "deleted"}

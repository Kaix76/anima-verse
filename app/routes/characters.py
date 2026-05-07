"""Character routes - Character management, images, and profile"""
import io
import os
import time
import mimetypes
import zipfile
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, StreamingResponse
from typing import Dict, Any, List, Optional
from app.core.log import get_logger

logger = get_logger("characters")

from app.models.account import set_current_character
from app.models.character import (
    list_available_characters,
    generate_random_appearance,
    get_character_appearance,
    save_character_appearance,
    get_character_personality,
    save_character_personality,
    get_character_current_location,
    save_character_current_location,
    get_character_current_activity,
    save_character_current_activity,
    get_character_current_feeling,
    save_character_current_feeling,
    get_character_outfits,
    add_character_outfit,
    delete_character_outfit,
    update_outfit_image,
    get_character_dir,
    get_character_images_dir,
    add_character_image,
    get_character_images,
    get_character_image_comments,
    add_character_image_comment,
    get_character_image_prompts,
    get_character_image_metadata,
    get_character_profile_image,
    set_character_profile_image,
    delete_character,
    delete_character_image,
    cleanup_orphaned_images,
    get_character_skill_config,
    save_character_skill_config,
    get_character_profile,
    save_character_profile,
    get_character_config,
    save_character_config,
    get_character_current_room,
    add_character_image_prompt,
    add_character_image_metadata,
    get_character_outfits_dir)
from app.core.dependencies import reload_skill_manager, get_skill_manager

router = APIRouter(prefix="/characters", tags=["characters"])


@router.get("/available-models")
def get_available_models() -> Dict[str, Any]:
    """Lists available models from all configured providers.

    Returns model lists grouped by provider, plus current task defaults.
    Used by the frontend for per-character model selection dropdowns.
    """
    from app.core.provider_manager import get_provider_manager
    from app.core.model_capabilities import get_model_capabilities

    pm = get_provider_manager()
    providers = pm.list_all_models()

    # Capabilities an jedes Model anhaengen
    for provider_name, provider_data in providers.items():
        for model in provider_data.get("models", []):
            model["capabilities"] = get_model_capabilities(model.get("name", ""))

    return {
        "providers": providers,
        "task_defaults": {},
    }


@router.get("/list")
def list_characters() -> Dict[str, Any]:
    """Listet alle verfuegbaren Characters (NPCs im System — nicht gefiltert).

    Avatar-Auswahl (wer der User spielen KANN) laeuft ueber /account/characters
    und ist dort nach allowed_characters gefiltert.
    """
    return {"characters": list_available_characters()}


@router.get("/at-location")
def characters_at_location(location: str, room: str = "") -> Dict[str, Any]:
    """Gibt alle Characters zurueck die sich am angegebenen Ort befinden.

    - `location`: ID oder Name der Location.
    - `room` (optional): wenn gesetzt, bekommen Characters ein `same_room`-
      Flag (true wenn sie in genau diesem Raum sind). Es wird **nicht**
      gefiltert — Frontend kann ausgegraut darstellen wer in einem anderen
      Raum derselben Location ist.
    """
    from app.models.world import resolve_location, get_room_by_id
    from app.models.character import get_character_current_room
    loc = resolve_location(location)
    loc_id = loc.get("id", "") if loc else ""
    loc_name = loc.get("name", location) if loc else location

    # Avatar-Raum auf {id, name} aufloesen — char_room kann historisch entweder
    # ID oder Name sein, daher matchen wir gegen beides.
    room_norm = (room or "").strip()
    avatar_room_id = ""
    avatar_room_name = ""
    if room_norm and loc:
        _r = get_room_by_id(loc, room_norm)
        if _r:
            avatar_room_id = _r.get("id", "") or room_norm
            avatar_room_name = _r.get("name", "")
        else:
            for _r in (loc.get("rooms") or []):
                if _r.get("name") == room_norm:
                    avatar_room_id = _r.get("id", "")
                    avatar_room_name = room_norm
                    break
            else:
                avatar_room_name = room_norm
                avatar_room_id = room_norm

    all_chars = list_available_characters()
    result = []
    for name in all_chars:
        char_loc = get_character_current_location(name)
        if not (char_loc and (char_loc == loc_id or char_loc == location)):
            continue
        char_room = (get_character_current_room(name) or "").strip()
        # Default same_room=True; nur wenn Avatar in einem Raum ist UND der
        # Character explizit in einem ANDEREN Raum, gilt er als "anderswo".
        # Character ohne Raum-Angabe = ueberall in der Location praesent.
        if room_norm and char_room:
            same_room = char_room in (avatar_room_id, avatar_room_name)
        else:
            same_room = True
        profile_img = get_character_profile_image(name)
        result.append({
            "name": name,
            "profile_image": profile_img or "",
            "avatar_url": f"/characters/{name}/images/{profile_img}" if profile_img else "",
            "same_room": same_room,
            "room": char_room,
        })
    return {"characters": result, "location": loc_name, "location_id": loc_id, "room": room_norm}


@router.get("/chatbots")
def list_chatbots() -> Dict[str, Any]:
    """Listet alle Chatbots (Characters ohne Location-System).

    Ein Chatbot ist ein Character dessen Template `locations_enabled: false`
    hat — er hat keine Weltposition und ist immer ansprechbar.
    """
    from app.models.character_template import is_feature_enabled
    all_chars = list_available_characters()
    result = []
    for name in all_chars:
        # Kein Location-System = Chatbot
        if is_feature_enabled(name, "locations_enabled"):
            continue
        profile_img = get_character_profile_image(name)
        result.append({
            "name": name,
            "profile_image": profile_img or "",
            "avatar_url": f"/characters/{name}/images/{profile_img}" if profile_img else "",
        })
    return {"characters": result}


@router.get("/animate/services")
async def list_animate_services() -> List[Dict[str, Any]]:
    """Liefert die verfuegbaren Animation-Services fuer das Frontend."""
    from app.skills.animate import get_animate_services
    return get_animate_services()


@router.get("/animate/available-loras")
def get_animate_available_loras() -> Dict[str, Any]:
    """Gibt verfuegbare LoRAs fuer Animation zurueck (WAN* gefiltert)."""
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"loras": ["None"]}
    all_loras = imagegen.get_cached_loras()
    # Nur LoRAs die mit WAN beginnen (case-insensitive)
    wan_loras = [l for l in all_loras if l.lower().startswith("wan")]
    return {"loras": ["None"] + wan_loras}


@router.get("/{character_name}/animate/loras")
def get_animate_loras(character_name: str) -> Dict[str, Any]:
    """Laedt gespeicherte Animation-LoRAs fuer einen Character."""
    config = get_character_skill_config(character_name, "animate") or {}
    return {
        "loras_high": config.get("loras_high"),
        "loras_low": config.get("loras_low"),
    }


@router.post("/{character_name}/animate/loras")
async def save_animate_loras(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert Animation-LoRAs (High + Low) pro Character.

    Body: {user_id, loras_high: [{name, strength}, ...], loras_low: [{name, strength}, ...]}
    """
    data = await request.json()
    user_id = data.get("user_id", "").strip()

    def _normalize(loras):
        if not loras:
            return None
        out = []
        for l in loras:
            name = (l.get("name") or "").strip() or "None"
            try:
                strength = float(l.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            out.append({"name": name, "strength": strength})
        return out

    config = get_character_skill_config(character_name, "animate") or {}
    loras_high = _normalize(data.get("loras_high"))
    loras_low = _normalize(data.get("loras_low"))
    if loras_high is not None:
        config["loras_high"] = loras_high
    if loras_low is not None:
        config["loras_low"] = loras_low
    save_character_skill_config(character_name, "animate", config)

    return {"status": "success"}


DEFAULT_NEW_CHARACTER_SKILLS = (
    # Skill-IDs aus app/skills/skill_manager.py:SKILL_REGISTRY.
    # Defaults fuer neu angelegte Characters — Liste entspricht den Haken
    # im Skills-Tab fuer einen "frisch geborenen" Character (alles Wesentliche
    # an, Spezial-/Nischen-Skills wie OutfitCreation, VideoGenerator,
    # Retrospect, MarkdownWriter, KnowledgeExtract bleiben aus, weil sie
    # Token-Kosten/Setup brauchen und nicht jeder NPC sie braucht).
    "imagegen", "setlocation", "talk_to", "send_message", "notify_user",
    "instagram_comment", "instagram_reply",
    "consume_item", "outfit_change", "setactivity",
)


@router.post("/create")
async def create_character(request: Request) -> Dict[str, Any]:
    """Erstellt einen neuen Character mit leerem Profil und zugewiesenem Template"""
    try:
        data = await request.json()
        character_name = data.get("character_name", "").strip()
        template_name = data.get("template", "human-default")
        if not character_name:
            raise HTTPException(status_code=400, detail="character_name fehlt")
        # Reservierte / problematische Namen abfangen — z.B. "undefined" oder
        # "null" entstehen wenn JS-Code irgendwo einen Wert nicht initialisiert
        # und ihn dann String-konvertiert. Das soll keinen Character-Ordner anlegen.
        if character_name.lower() in ("undefined", "null", "none", "nan"):
            raise HTTPException(status_code=400,
                detail=f"'{character_name}' ist als Character-Name nicht erlaubt")

        # Check if character already exists
        existing = list_available_characters()
        if character_name in existing:
            raise HTTPException(status_code=409, detail=f"Character '{character_name}' existiert bereits")

        # Create character with initial profile + template reference
        initial_profile = {
            "character_name": character_name,
            "template": template_name,
        }
        save_character_profile(character_name, initial_profile)

        # Skill-Defaults schreiben — ohne diese Files greift die ALWAYS_LOAD-
        # Filter-Logik (skill_manager._get_agent_skills) und schaltet alle
        # Skills standardmaessig AUS. Mit der Default-Liste hat der frische
        # Char direkt das uebliche Repertoire (chat, set_location, magic
        # konsumieren, outfit-wechsel, ...).
        for _sid in DEFAULT_NEW_CHARACTER_SKILLS:
            try:
                save_character_skill_config(character_name, _sid, {"enabled": True})
            except Exception as _e:
                logger.warning("create_character: skill-default '%s' nicht gesetzt: %s",
                               _sid, _e)

        # Auto-assign the new character to the creator's allowed_characters list
        # so they can immediately see and use it without a separate admin step.
        from app.core.auth_dependency import get_current_user_optional
        from app.core.users import update_user
        creator = get_current_user_optional(request)
        if creator and creator.get("id"):
            allowed = list(creator.get("allowed_characters") or [])
            if character_name not in allowed:
                allowed.append(character_name)
                try:
                    update_user(creator["id"], allowed_characters=allowed)
                except Exception as e:
                    logger.warning(
                        "create_character: konnte allowed_characters fuer "
                        "user=%s nicht aktualisieren: %s",
                        creator.get("username"), e,
                    )

        # Set as current character
        set_current_character(character_name)

        return {
            "status": "success",
            "character": character_name,
            "template": template_name,
            "message": f"Character '{character_name}' erstellt"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/generate-appearance")
def generate_character_appearance(character_name: str) -> Dict[str, Any]:
    """Generiert ein zufaelliges Aussehen"""
    appearance = generate_random_appearance()
    return {"character": character_name, "appearance": appearance}


@router.get("/{character_name}/appearance")
def get_character_appearance_route(character_name: str) -> Dict[str, Any]:
    """Ruft das aktuelle Aussehen ab"""
    appearance = get_character_appearance(character_name)
    return {"character": character_name, "appearance": appearance or ""}


@router.post("/{character_name}/appearance")
async def update_character_appearance(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert das Aussehen"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        appearance = data.get("appearance", "")
        if not appearance:
            raise HTTPException(status_code=400, detail="Aussehen darf nicht leer sein")

        save_character_appearance(character_name, appearance)
        return {"status": "success", "character": character_name, "appearance": appearance}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/personality")
def get_character_personality_route(character_name: str) -> Dict[str, Any]:
    """Ruft die aktuelle Persoenlichkeit ab"""
    personality = get_character_personality(character_name)
    return {"character": character_name, "personality": personality or ""}


@router.post("/{character_name}/personality")
async def update_character_personality(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert die Persoenlichkeit"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        personality = data.get("personality", "")
        if not personality:
            raise HTTPException(status_code=400, detail="Persoenlichkeit darf nicht leer sein")

        save_character_personality(character_name, personality)
        return {"status": "success", "character": character_name, "personality": personality}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/current-location")
def get_character_current_location_route(character_name: str) -> Dict[str, Any]:
    """Gibt den aktuellen virtuellen Aufenthaltsort zurueck"""
    from app.models.world import get_location_name as _get_loc_name, get_room_by_id, _load_world_data
    from app.models.character_template import is_feature_enabled
    locations_on = is_feature_enabled(character_name, "locations_enabled")
    activities_on = is_feature_enabled(character_name, "activities_enabled")
    location_id = get_character_current_location(character_name) if locations_on else ""
    location_name = _get_loc_name(location_id) if location_id else ""
    activity = get_character_current_activity(character_name) if activities_on else ""
    from app.models.character import get_character_current_room
    current_room = get_character_current_room(character_name) if locations_on else ""
    # Resolve room: could be an ID or a name — normalize to ID + name
    current_room_id = ""
    current_room_name = ""
    if current_room and location_id:
        world_data = _load_world_data()
        for loc in world_data.get("locations", []):
            if loc.get("id") == location_id:
                # Try by ID first
                room = get_room_by_id(loc, current_room)
                if room:
                    current_room_id = room.get("id", current_room)
                    current_room_name = room.get("name", "")
                else:
                    # Fallback: match by name
                    for r in loc.get("rooms", []):
                        if r.get("name", "").lower() == current_room.lower():
                            current_room_id = r.get("id", "")
                            current_room_name = r.get("name", "")
                            break
                break
    # Detail-Beschreibung der Aktivitaet
    from app.models.character import get_character_profile, get_movement_target
    profile = get_character_profile(character_name)
    activity_detail = profile.get("current_activity_detail", "") if activities_on else ""
    movement_target_id = get_movement_target(character_name) if locations_on else ""
    movement_target_name = _get_loc_name(movement_target_id) if movement_target_id else ""
    return {
        "character": character_name,
        "current_location": location_name or location_id or "",
        "current_location_id": location_id or "",
        "current_activity": activity or "",
        "current_activity_detail": activity_detail,
        "current_room": current_room_id or current_room or "",
        "current_room_name": current_room_name or current_room or "",
        "movement_target_id": movement_target_id,
        "movement_target_name": movement_target_name,
    }


@router.post("/{character_name}/current-location")
async def update_character_current_location(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert den aktuellen virtuellen Aufenthaltsort"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        location = data.get("current_location", "")
        room = data.get("current_room", "")

        # Name → ID aufloesen falls noetig
        from app.models.world import resolve_location as _resolve_loc
        from app.models.character import get_character_current_location, get_character_current_room, save_character_current_room, save_character_current_activity
        loc_obj = _resolve_loc(location)
        location_to_save = loc_obj["id"] if loc_obj and loc_obj.get("id") else location
        location_name_resp = loc_obj.get("name", location) if loc_obj else location
        old_loc = get_character_current_location(character_name)
        old_room = get_character_current_room(character_name) or ""
        # Avatar: Outfit NICHT automatisch umstellen (manuelle User-Wahl bleibt).
        from app.models.account import get_active_character
        _is_avatar = (get_active_character() == character_name)
        save_character_current_location(character_name, location_to_save,
            _skip_compliance=_is_avatar)
        # Raum und Aktivitaet: bei Ortswechsel loeschen, es sei denn Raum wurde mitgegeben
        if location_to_save != old_loc:
            save_character_current_room(character_name, room or '')
            if not room:
                save_character_current_activity(character_name, '')
        elif room:
            save_character_current_room(character_name, room)

        # Avatar-Eintritts-Hook: andere Characters im neuen Raum bemerken den
        # Eintritt und reagieren ggf. (forced_thought + TalkTo). Nur wenn:
        # - es der Avatar ist (sonst ist's der Auto-Move eines NPCs)
        # - tatsaechlich Raum oder Location gewechselt hat (nicht nur Re-Save)
        new_room = room or ''
        room_changed = (location_to_save != old_loc) or (new_room and new_room != old_room)
        from app.core.log import get_logger as _gl_route
        _gl_route("characters_route").info(
            "current-location POST: char=%s is_avatar=%s old_loc=%s -> new_loc=%s old_room=%s -> new_room=%s room_changed=%s",
            character_name, _is_avatar, old_loc, location_to_save, old_room, new_room, room_changed)
        room_entry_result = {"reactor": "", "silent_noticers": []}
        if _is_avatar and room_changed:
            try:
                from app.core.room_entry import on_avatar_room_entry
                # Room-Label aufloesen
                _room_label = ""
                if new_room and loc_obj:
                    from app.models.world import get_room_by_id
                    _r = get_room_by_id(loc_obj, new_room)
                    if _r and _r.get("name"):
                        _room_label = _r["name"]
                import asyncio as _asyncio
                room_entry_result = await _asyncio.to_thread(
                    on_avatar_room_entry,
                    avatar_name=character_name,
                    location_id=location_to_save,
                    room_id=new_room,
                    location_label=location_name_resp,
                    room_label=_room_label,
                ) or room_entry_result
            except Exception as _re:
                from app.core.log import get_logger as _gl
                _gl("characters_route").debug("avatar_room_entry hook failed: %s", _re)

        return {
            "status": "success",
            "character": character_name,
            "current_location": location_name_resp,
            "reactor": room_entry_result.get("reactor", ""),
            "silent_noticers": room_entry_result.get("silent_noticers", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/place-on-map")
async def place_character_on_map(character_name: str, request: Request) -> Dict[str, Any]:
    """Drag&Drop-Platzierung: setzt current_location UND fuegt die Location
    in die known_locations-Liste des Characters ein. Damit aktiviert der erste
    Drop strict-mode (Listen-basierte Sichtbarkeit) — bis dahin ist der
    Character auf Legacy-Verhalten (knowledge_item-Gating only).
    """
    try:
        data = await request.json()
        location = (data.get("location_id") or data.get("current_location") or "").strip()
        room = (data.get("current_room") or "").strip()
        if not location:
            raise HTTPException(status_code=400, detail="location_id fehlt")

        from app.models.world import resolve_location as _resolve_loc
        from app.models.character import (
            add_known_location, get_character_current_room,
            save_character_current_room, save_character_current_activity)

        loc_obj = _resolve_loc(location)
        location_to_save = loc_obj["id"] if loc_obj and loc_obj.get("id") else location
        location_name_resp = loc_obj.get("name", location) if loc_obj else location

        add_known_location(character_name, location_to_save)

        old_loc = get_character_current_location(character_name)
        old_room = get_character_current_room(character_name) or ""
        from app.models.account import get_active_character
        _is_avatar = (get_active_character() == character_name)
        save_character_current_location(character_name, location_to_save,
            _skip_compliance=_is_avatar)
        if location_to_save != old_loc:
            save_character_current_room(character_name, room or '')
            if not room:
                save_character_current_activity(character_name, '')
        elif room:
            save_character_current_room(character_name, room)

        new_room = room or ''
        room_changed = (location_to_save != old_loc) or (new_room and new_room != old_room)
        room_entry_result = {"reactor": "", "silent_noticers": []}
        if _is_avatar and room_changed:
            try:
                from app.core.room_entry import on_avatar_room_entry
                _room_label = ""
                if new_room and loc_obj:
                    from app.models.world import get_room_by_id
                    _r = get_room_by_id(loc_obj, new_room)
                    if _r and _r.get("name"):
                        _room_label = _r["name"]
                import asyncio as _asyncio
                room_entry_result = await _asyncio.to_thread(
                    on_avatar_room_entry,
                    avatar_name=character_name,
                    location_id=location_to_save,
                    room_id=new_room,
                    location_label=location_name_resp,
                    room_label=_room_label,
                ) or room_entry_result
            except Exception as _re:
                logger.debug("avatar_room_entry hook failed: %s", _re)

        return {
            "status": "success",
            "character": character_name,
            "current_location": location_name_resp,
            "current_location_id": location_to_save,
            "reactor": room_entry_result.get("reactor", ""),
            "silent_noticers": room_entry_result.get("silent_noticers", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/current-activity")
def get_character_current_activity_route(character_name: str) -> Dict[str, Any]:
    """Gibt die aktuelle Aktivitaet zurueck"""
    activity = get_character_current_activity(character_name)
    return {"character": character_name, "current_activity": activity or ""}


@router.post("/{character_name}/current-activity")
async def update_character_current_activity(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert die aktuelle Aktivitaet"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        activity = data.get("current_activity", "")

        save_character_current_activity(character_name, activity)

        # Effects werden zeitproportional via save_character_current_activity
        # und hourly_status_tick angewendet (nicht mehr sofort).

        # Room automatisch aus Activity ableiten — ABER NICHT wenn der
        # Character gerade im aktiven Chat des Users ist. Sonst springt Kira
        # mitten im Pool-RP plotzlich ins Schlafzimmer wenn der LLM-Stream
        # ein "**I do** Sex" emittiert. Avatar wird ebenso nie verschoben.
        room_name = ""
        room_id = ""
        if activity:
            from app.models.character import save_character_current_room
            from app.models.world import find_room_by_activity, get_location
            from app.models.account import is_player_controlled
            try:
                from app.models.account import get_chat_partner
                _is_chat_partner = (get_chat_partner() == character_name)
            except Exception:
                _is_chat_partner = False
            _skip_room_move = _is_chat_partner or is_player_controlled(character_name)
            if _skip_room_move:
                logger.info("update_current_activity: Auto-Raum-Move fuer %s "
                             "uebersprungen (chat_partner=%s, avatar=%s)",
                             character_name, _is_chat_partner,
                             is_player_controlled(character_name))
            else:
                loc_id = get_character_current_location(character_name)
                if loc_id:
                    loc_data = get_location(loc_id)
                    if loc_data:
                        room = find_room_by_activity(loc_data, activity)
                        if room:
                            room_id = room.get("id", "")
                            save_character_current_room(character_name, room_id)
                            room_name = room.get("name", "")

        return {"status": "success", "character": character_name, "current_activity": activity, "current_room": room_name, "current_room_id": room_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/current-feeling")
def get_character_current_feeling_route(character_name: str) -> Dict[str, Any]:
    """Gibt das aktuelle Gefuehl zurueck"""
    feeling = get_character_current_feeling(character_name)
    return {"character": character_name, "current_feeling": feeling or ""}


@router.post("/{character_name}/current-feeling")
async def update_character_current_feeling(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert das aktuelle Gefuehl"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        feeling = data.get("current_feeling", "")

        save_character_current_feeling(character_name, feeling)

        return {"status": "success", "character": character_name, "current_feeling": feeling}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/status-effects")
def get_status_effects_route(
    character_name: str) -> Dict[str, Any]:
    """Gibt die aktuellen Status-Werte eines Characters zurueck.

    Beim ersten Aufruf: Initialisiert status_effects aus Trait-Defaults
    und persistiert sie im Profil. Danach werden immer die gespeicherten
    Werte zurueckgegeben.
    """
    try:
        from app.models.character_template import is_feature_enabled
        if not is_feature_enabled(character_name, "status_effects_enabled"):
            return {"status_effects": {}, "traits": {}, "bar_meta": {}}
        from app.models.character import get_character_profile, get_character_config, save_character_profile
        profile = get_character_profile(character_name)
        config = get_character_config(character_name)
        status = profile.get("status_effects", {})

        # Template laden — Quelle fuer Stat-Defaults und Bar-Metadaten
        bar_meta = {}
        stat_defaults = {}  # stat_key -> default_value aus Template
        try:
            from app.models.character_template import get_template
            template_name = profile.get("template", "human-default")
            template = get_template(template_name)
            if template:
                for section in template.get("sections", []):
                    for field in section.get("fields", []):
                        if field.get("store") != "status_effects":
                            continue
                        stat_key = field.get("key", "")
                        if not stat_key:
                            continue
                        # Default aus Template
                        if field.get("default") is not None:
                            try:
                                stat_defaults[stat_key] = int(field["default"])
                            except (ValueError, TypeError):
                                pass
                        # Bar-Metadaten
                        meta = {}
                        if field.get("bar_color"):
                            meta["color"] = field["bar_color"]
                        if field.get("bar_label"):
                            meta["label"] = field["bar_label"]
                        if field.get("label"):
                            meta["name"] = field["label"]
                        if field.get("label_de"):
                            meta["name_de"] = field["label_de"]
                        if meta:
                            bar_meta[stat_key] = meta
        except Exception:
            pass

        # Fehlende status_effects aus Template-Defaults initialisieren und persistieren
        status_changed = False
        for stat_key, stat_default in stat_defaults.items():
            if stat_key not in status:
                status[stat_key] = stat_default
                status_changed = True

        if status_changed:
            profile["status_effects"] = status
            save_character_profile(character_name, profile)

        return {"status_effects": status, "bar_meta": bar_meta}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Template-Wechsel ===

@router.post("/{character_name}/switch-template")
async def switch_template_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Template-Wechsel mit Diff: Zeigt neue/wegfallende Felder und fuehrt Migration durch.

    mode="diff": Gibt nur den Diff zurueck (keine Aenderung)
    mode="apply": Fuehrt den Wechsel durch (neue Defaults setzen, alte Felder loeschen)
    """
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        new_template_name = data.get("new_template", "").strip()
        mode = data.get("mode", "diff")

        if not new_template_name:
            raise HTTPException(status_code=400, detail="user_id und new_template erforderlich")

        from app.models.character import get_character_profile, get_character_config, save_character_profile, save_character_config
        from app.models.character_template import get_template

        profile = get_character_profile(character_name)
        config = get_character_config(character_name)
        old_template_name = profile.get("template", "")
        old_template = get_template(old_template_name) if old_template_name else None
        new_template = get_template(new_template_name)

        if not new_template:
            raise HTTPException(status_code=404, detail=f"Template '{new_template_name}' nicht gefunden")

        # ALLE Felder aus altem und neuem Template sammeln (nicht nur traits)
        def _collect_all_fields(tmpl):
            fields = {}
            if not tmpl:
                return fields
            for section in tmpl.get("sections", []):
                for field in section.get("fields", []):
                    fkey = field.get("key", "")
                    if fkey:
                        fields[fkey] = field
            return fields

        old_fields = _collect_all_fields(old_template)
        new_fields = _collect_all_fields(new_template)

        # Diff berechnen
        added = []
        for key, field in new_fields.items():
            if key not in old_fields:
                added.append({
                    "key": key,
                    "label": field.get("label", key),
                    "label_de": field.get("label_de", ""),
                    "default": field.get("default"),
                    "store": field.get("store", ""),
                    "is_stat": field.get("store") == "status_effects",
                })

        removed = []
        for key, field in old_fields.items():
            if key not in new_fields:
                store = field.get("store", "")
                # Aktuellen Wert aus dem richtigen Speicher lesen
                if store == "status_effects":
                    current_val = profile.get("status_effects", {}).get(key, "")
                elif store == "config":
                    current_val = config.get(key, "")
                else:
                    current_val = profile.get(key, "")
                removed.append({
                    "key": key,
                    "label": field.get("label", key),
                    "label_de": field.get("label_de", ""),
                    "current_value": current_val,
                    "store": store,
                    "is_stat": store == "status_effects",
                })

        if mode == "diff":
            return {
                "old_template": old_template_name,
                "new_template": new_template_name,
                "added": added,
                "removed": removed,
            }

        # mode == "apply": Migration durchfuehren

        # 1. Neue Felder mit Defaults fuellen
        status = profile.get("status_effects", {})
        for item in added:
            default_val = item.get("default")
            key = item["key"]
            store = item.get("store", "")
            if store == "status_effects":
                if default_val is not None and key not in status:
                    status[key] = default_val
            elif store == "config":
                config[key] = default_val if default_val is not None else ""
            else:
                profile[key] = default_val if default_val is not None else ""

        # 2. Alte Felder entfernen
        for item in removed:
            key = item["key"]
            store = item.get("store", "")
            if store == "status_effects":
                status.pop(key, None)
                config.pop(key + "_hourly", None)
            elif store == "config":
                config.pop(key, None)
            else:
                profile.pop(key, None)

        profile["status_effects"] = status

        # 4. Template im Profil setzen
        profile["template"] = new_template_name

        # 5. Speichern
        save_character_profile(character_name, profile)
        save_character_config(character_name, config)

        return {
            "ok": True,
            "old_template": old_template_name,
            "new_template": new_template_name,
            "added": added,
            "removed": removed,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Erlaubte Orte ===

@router.get("/{character_name}/home-location")
def get_home_location_route(
    character_name: str) -> Dict[str, Any]:
    """Gibt Home-Location und Home-Room eines Characters zurueck."""
    if not character_name:
        return {"home_location": "", "home_room": ""}
    from app.models.character import get_character_config
    config = get_character_config(character_name)
    return {
        "home_location": config.get("home_location", ""),
        "home_room": config.get("home_room", ""),
    }


@router.post("/{character_name}/home-location")
async def save_home_location_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert Home-Location und Home-Room eines Characters."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "").strip()
        if not character_name:
            raise HTTPException(status_code=400, detail="user_id und character_name fehlen")
        from app.models.character import get_character_config, save_character_config
        config = get_character_config(character_name)
        config["home_location"] = data.get("home_location", "")
        config["home_room"] = data.get("home_room", "")
        save_character_config(character_name, config)
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/outfit-lora-options")
def get_outfit_lora_options(character_name: str = "") -> Dict[str, Any]:
    """Liefert die LoRA-Liste fuer den Outfit-Piece-Editor, gefiltert nach
    dem aktiven Outfit-Image-Workflow.

    Workflow-Aufloesung (in Reihenfolge):
    1. Character-Override profile.outfit_imagegen.workflow (wenn character_name uebergeben)
    2. Erster verfuegbarer Comfy-Workflow als Default

    Response:
        workflow: name des aufgeloesten Workflows
        filter: Glob-Filter dieses Workflows (informativ)
        loras: gefilterte LoRA-Liste (ohne 'None')
    """
    import fnmatch
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"workflow": "", "filter": "", "loras": []}
    workflows = imagegen.get_comfy_workflows() or []

    # Workflow-Aufloesung
    active_wf_name = ""
    if character_name:
        try:
            from app.models.character import get_character_profile
            prof = get_character_profile(character_name) or {}
            active_wf_name = ((prof.get("outfit_imagegen") or {}).get("workflow") or "").strip()
        except Exception:
            pass
    if not active_wf_name:
        # Ersten verfuegbaren Workflow waehlen — unverfuegbare ueberspringen
        for wf in workflows:
            if wf.get("available", True):
                active_wf_name = wf.get("name", "")
                break
        if not active_wf_name and workflows:
            active_wf_name = workflows[0].get("name", "")

    # Filter holen
    flt = ""
    for wf in workflows:
        if wf.get("name") == active_wf_name:
            flt = (wf.get("filter") or "").strip()
            break

    all_loras = imagegen.get_cached_loras() or []
    if flt:
        filtered = [l for l in all_loras if fnmatch.fnmatch(l, flt)]
    else:
        filtered = list(all_loras)

    return {
        "workflow": active_wf_name,
        "filter": flt,
        "loras": filtered,
    }


@router.get("/outfit-rules")
def get_outfit_rules_route() -> Dict[str, Any]:
    """Liefert die outfit_rules.json fuer das UI (Slot-Matrix)."""
    from app.core.outfit_rules import _load_rules
    rules = _load_rules()
    return {
        "outfit_types": {
            k: {"required": v.get("required", [])}
            for k, v in (rules.get("outfit_types") or {}).items()
            if not k.startswith("_")
        },
    }


@router.get("/{character_name}/outfit-exceptions")
def get_outfit_exceptions(character_name: str) -> Dict[str, Any]:
    """Liefert die outfit_exceptions des Characters."""
    from app.models.character import get_character_profile
    profile = get_character_profile(character_name) or {}
    return {"exceptions": profile.get("outfit_exceptions") or {}}


@router.put("/{character_name}/outfit-exceptions")
async def set_outfit_exceptions(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert outfit_exceptions — dict outfit_type -> {skip_required: [slots], reason: ""}."""
    from app.models.character import get_character_profile, save_character_profile
    body = await request.json()
    exceptions = body.get("exceptions") or {}
    # Nur gueltige Struktur speichern
    cleaned = {}
    for otype, entry in exceptions.items():
        if not isinstance(entry, dict):
            continue
        skip = entry.get("skip_required") or []
        if not isinstance(skip, list):
            continue
        if skip or (entry.get("reason") or "").strip():
            cleaned[otype] = {
                "skip_required": [s for s in skip if isinstance(s, str) and s],
                "reason": (entry.get("reason") or "").strip(),
            }
    profile = get_character_profile(character_name) or {}
    if cleaned:
        profile["outfit_exceptions"] = cleaned
    else:
        profile.pop("outfit_exceptions", None)
    save_character_profile(character_name, profile)
    return {"status": "ok", "exceptions": cleaned}


@router.get("/{character_name}/outfit-compliance")
def get_outfit_compliance(character_name: str) -> Dict[str, Any]:
    """Prueft das aktuelle Outfit des Characters gegen die outfit_type-Regeln.

    Liefert: aktueller outfit_type (aus Location/Room), required_slots,
    equipped_slots, missing (required - equipped), exceptions (aus Profil).
    """
    from app.core.outfit_rules import baseline_required_slots, resolve_required_slots
    from app.models.inventory import get_equipped_pieces
    from app.models.character import (
        get_character_profile, get_character_current_location)
    from app.models.world import resolve_location

    profile = get_character_profile(character_name) or {}
    equipped = get_equipped_pieces(character_name)

    # Aktueller outfit_type: aus Location/Room ableiten
    loc_id = get_character_current_location(character_name)
    loc = resolve_location(loc_id) if loc_id else None
    outfit_type = ""
    room_name = ""
    if loc:
        # Falls Character in einem Raum ist, Raum-outfit_type bevorzugen
        room_id = (profile.get("current_room") or "").strip()
        for r in (loc.get("rooms") or []):
            if r.get("id") == room_id:
                outfit_type = (r.get("outfit_type") or "").strip()
                room_name = r.get("name", "")
                break
        if not outfit_type:
            outfit_type = (loc.get("outfit_type") or "").strip()

    baseline = baseline_required_slots(outfit_type)
    required = sorted(resolve_required_slots(outfit_type, character_name))
    equipped_slots = sorted(s for s, iid in equipped.items() if iid)
    # Gecoverte Slots (z.B. bottom via Kleid) zaehlen als belegt
    from app.models.character import _collect_covered_slots
    covered_slots = _collect_covered_slots(equipped)
    covered_or_equipped = set(equipped_slots) | covered_slots
    missing = sorted(set(required) - covered_or_equipped)
    # Case-insensitive Lookup fuer Display
    _exc_all = profile.get("outfit_exceptions") or {}
    _key = (outfit_type or "").strip().lower()
    exceptions = {}
    for _k, _v in _exc_all.items():
        if isinstance(_k, str) and _k.strip().lower() == _key:
            exceptions = _v or {}
            break

    return {
        "character": character_name,
        "location": loc.get("name", "") if loc else "",
        "room": room_name,
        "outfit_type": outfit_type,
        "baseline_required": sorted(baseline),
        "required": required,
        "equipped": equipped_slots,
        "covered": sorted(covered_slots),
        "missing": missing,
        "exceptions": exceptions,
    }


@router.get("/{character_name}/outfits")
def get_character_outfits_route(character_name: str) -> Dict[str, Any]:
    """Gibt alle definierten Outfits zurueck"""
    outfits = get_character_outfits(character_name)
    return {"character": character_name, "outfits": outfits}


@router.post("/{character_name}/outfits")
async def add_character_outfit_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Fuegt ein neues Outfit hinzu oder aktualisiert ein bestehendes.

    Akzeptiert neues Format: {user_id, id?, name, outfit, locations[], activities[]}
    """
    try:
        data = await request.json()
        user_id = data.get("user_id", "")

        outfit_data = {
            "id": data.get("id", ""),
            "name": data.get("name", ""),
            "outfit": data.get("outfit", ""),
            "pieces": data.get("pieces", []),
            "remove_slots": data.get("remove_slots", []),
            "pieces_colors": data.get("pieces_colors", {}),
            "locations": data.get("locations", []),
            "activities": data.get("activities", []),
            "excluded_locations": data.get("excluded_locations", []),
        }

        outfit_id = add_character_outfit(character_name, outfit_data)
        return {"status": "success", "character": character_name, "id": outfit_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{character_name}")
async def delete_character_route(character_name: str) -> Dict[str, Any]:
    """Loescht einen Character vollstaendig (DB + Storage-Verzeichnis)."""
    if not character_name:
        raise HTTPException(status_code=400, detail="character_name fehlt")
    success = delete_character(character_name)
    if not success:
        raise HTTPException(status_code=404,
                            detail=f"Character '{character_name}' nicht gefunden oder geschuetzt")
    return {"status": "success", "deleted": character_name}


@router.delete("/{character_name}/outfits")
async def delete_character_outfit_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Loescht ein Outfit per ID."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        outfit_id = data.get("id", "")
        if not outfit_id:
            raise HTTPException(status_code=400, detail="id fehlt")

        success = delete_character_outfit(character_name, outfit_id=outfit_id)
        if success:
            return {"status": "success", "character": character_name, "id": outfit_id}
        else:
            raise HTTPException(status_code=404, detail="Outfit nicht gefunden")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/generate-profile-image")
async def generate_profile_image_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Generiert ein neues Profilbild via ImageGenerationSkill."""
    import os
    import json as _json
    data = await request.json()
    user_id = data.get("user_id", "")

    # Character-Profil laden: Appearance (Tokens auflösen)
    from app.models.character import get_character_profile, get_character_appearance, set_character_profile_image
    from app.models.character_template import resolve_profile_tokens, get_template
    profile = get_character_profile(character_name)
    tmpl = get_template(profile.get("template", "")) if profile.get("template") else None
    appearance = get_character_appearance(character_name)
    if appearance and "{" in appearance:
        appearance = resolve_profile_tokens(appearance, profile, template=tmpl, target_key="character_appearance")

    # Prompt aus Dialog oder Standard-Prefix + Appearance
    prompt_text = data.get("prompt", "").strip()
    if not prompt_text:
        prefix = os.environ.get("PROFILE_IMAGE_PROMPT_PREFIX", "photorealistic, portrait, only head,")
        parts = [prefix]
        if appearance:
            parts.append(appearance)
        prompt_text = ", ".join(p.strip(", ") for p in parts if p.strip())

    # ImageGenerationSkill holen
    try:
        skill_manager = get_skill_manager()
        image_skill = skill_manager.get_skill("image_generation")
    except Exception:
        image_skill = None
    if not image_skill:
        raise HTTPException(status_code=500, detail="ImageGenerationSkill nicht verfuegbar")

    # Workflow/Backend/Modell aus Request
    workflow_name = data.get("workflow", "").strip()
    backend_name = data.get("backend", "").strip()
    loras_override = data.get("loras")
    model_override = data.get("model_override", "").strip()

    payload = {
        "prompt": prompt_text,
        "agent_name": character_name,
        "user_id": "",
        "auto_enhance": False,
        "force_faceswap": False,
        "set_profile": True,
        "workflow": workflow_name,
        "backend": backend_name,
    }
    if loras_override is not None:
        payload["loras"] = loras_override
    if model_override:
        payload["model_override"] = model_override
    input_data = _json.dumps(payload)

    try:
        import asyncio
        result = await asyncio.to_thread(image_skill.execute, input_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bildgenerierung fehlgeschlagen: {str(e)}")

    # Dateiname aus dem Ergebnis extrahieren
    import re
    image_match = re.search(r'/characters/[^/]+/images/([^?)\n]+)', result)
    if not image_match:
        raise HTTPException(status_code=500, detail=f"Kein Bild im Ergebnis: {result[:200]}")

    image_filename = image_match.group(1)

    # Als Profilbild setzen
    set_character_profile_image(character_name, image_filename)

    image_url = f"/characters/{character_name}/images/{image_filename}"
    return {"status": "success", "image": image_filename, "image_url": image_url}


def _build_outfit_image_prompt(character_name: str, outfit_description: str) -> str:
    """Baut den Prompt fuer ein Outfit-Bild (separiert: character + outfit + pose + expression)."""
    import os
    from app.core.prompt_builder import PromptBuilder
    from app.core.expression_pose_maps import DEFAULT_EXPRESSION, DEFAULT_POSE

    _builder = PromptBuilder(character_name)
    _persons = _builder.detect_persons("", character_names=[character_name])
    _actor = _persons[0].actor_label if _persons else character_name
    _appearance = _persons[0].appearance if _persons else ""

    prefix = os.environ.get("OUTFIT_IMAGE_PROMPT_PREFIX", "full body portrait")
    character_prompt = f"{_actor}, {_appearance}"
    outfit_prompt = f"{_actor} is wearing {outfit_description}"
    return ", ".join(p for p in [
        prefix, character_prompt, outfit_prompt, DEFAULT_POSE, DEFAULT_EXPRESSION,
    ] if p)


@router.get("/{character_name}/outfits/{outfit_id}/image-prompt")
async def get_outfit_image_prompt(character_name: str, outfit_id: str) -> Dict[str, str]:
    """Berechnet den Prompt fuer ein Outfit-Bild (Vorschau fuer Dialog)."""

    outfits = get_character_outfits(character_name)
    outfit_obj = next((o for o in outfits if o.get("id") == outfit_id), None)
    if not outfit_obj:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden")

    outfit_description = outfit_obj.get("outfit", "")
    from app.models.character_template import resolve_profile_tokens, get_template
    profile = get_character_profile(character_name)
    tmpl = get_template(profile.get("template", "")) if profile.get("template") else None
    if outfit_description and "{" in outfit_description:
        outfit_description = resolve_profile_tokens(outfit_description, profile, template=tmpl, target_key="outfit")

    return {"prompt": _build_outfit_image_prompt(character_name, outfit_description)}


@router.post("/{character_name}/outfits/{outfit_id}/generate-image")
async def generate_outfit_image_route(character_name: str, outfit_id: str, request: Request) -> Dict[str, Any]:
    """Generiert ein Bild fuer ein Outfit via ImageGenerationSkill."""
    import os
    import json as _json
    data = await request.json()
    user_id = data.get("user_id", "")

    # Outfit suchen
    outfits = get_character_outfits(character_name)
    outfit_obj = next((o for o in outfits if o.get("id") == outfit_id), None)
    if not outfit_obj:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden")

    outfit_description = outfit_obj.get("outfit", "")
    if not outfit_description:
        raise HTTPException(status_code=400, detail="Outfit hat keine Beschreibung")

    # Character-Profil laden: Appearance + Geschlecht (Tokens auflösen)
    from app.models.character import get_character_profile, get_character_appearance
    from app.models.character_template import resolve_profile_tokens, get_template
    profile = get_character_profile(character_name)
    tmpl = get_template(profile.get("template", "")) if profile.get("template") else None
    appearance = get_character_appearance(character_name)
    if appearance and "{" in appearance:
        appearance = resolve_profile_tokens(appearance, profile, template=tmpl, target_key="character_appearance")
    if outfit_description and "{" in outfit_description:
        outfit_description = resolve_profile_tokens(outfit_description, profile, template=tmpl, target_key="outfit")

    # Prompt: aus Dialog (wenn vorhanden) oder via Hilfsfunktion aufbauen
    prompt_text = data.get("prompt", "").strip()
    if not prompt_text:
        prompt_text = _build_outfit_image_prompt(character_name, outfit_description)

    # ImageGenerationSkill holen
    try:
        skill_manager = get_skill_manager()
        image_skill = skill_manager.get_skill("image_generation")
    except Exception:
        image_skill = None

    if not image_skill:
        raise HTTPException(status_code=500, detail="ImageGenerationSkill nicht verfuegbar")

    # Workflow/Backend/LoRA/Modell-Auswahl aus Request (Fallback: OUTFIT_IMAGEGEN_DEFAULT)
    workflow_name = data.get("workflow", "").strip()
    backend_name = data.get("backend", "").strip()
    if not workflow_name and not backend_name:
        _outfit_default = os.environ.get("OUTFIT_IMAGEGEN_DEFAULT", "").strip()
        if _outfit_default.startswith("workflow:"):
            workflow_name = _outfit_default[len("workflow:"):].strip()
        elif _outfit_default.startswith("backend:"):
            backend_name = _outfit_default[len("backend:"):].strip()
    loras_override = data.get("loras")
    model_override = data.get("model_override", "").strip()

    # Auflösung aus .env (Hochformat für Ganzkörper-Outfits)
    outfit_w = int(os.environ.get("OUTFIT_IMAGE_WIDTH", 0) or 0) or None
    outfit_h = int(os.environ.get("OUTFIT_IMAGE_HEIGHT", 0) or 0) or None

    # Appearance mitgeben damit Reference-Bilder aufgeloest werden (Flux2 braucht sie)
    payload = {
        "prompt": prompt_text,
        "agent_name": character_name,
        "user_id": "",
        "auto_enhance": False,
        "force_faceswap": True,
        "skip_gallery": True,
        "workflow": workflow_name,
        "backend": backend_name,
        "appearances": [{"name": character_name, "appearance": appearance or ""}],
        "profile_only": True,
    }
    if outfit_w:
        payload["override_width"] = outfit_w
    if outfit_h:
        payload["override_height"] = outfit_h
    if loras_override is not None:
        payload["loras"] = loras_override
    if model_override:
        payload["model_override"] = model_override
    input_data = _json.dumps(payload)

    try:
        import asyncio
        result = await asyncio.to_thread(image_skill.execute, input_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bildgenerierung fehlgeschlagen: {str(e)}")

    # Dateiname aus dem Ergebnis extrahieren (Skill gibt Markdown-Links zurueck)
    import re
    image_match = re.search(r'/characters/[^/]+/images/([^?)\n]+)', result)
    if not image_match:
        raise HTTPException(status_code=500, detail=f"Kein Bild im Ergebnis: {result[:200]}")

    image_filename = image_match.group(1)

    # Generiertes Bild von images/ nach outfits/ verschieben
    from app.models.character import get_character_outfits_dir, postprocess_outfit_image
    import shutil
    images_dir = get_character_images_dir(character_name)
    outfits_dir = get_character_outfits_dir(character_name)
    src_path = images_dir / image_filename
    dst_path = outfits_dir / image_filename
    if src_path.exists():
        shutil.move(str(src_path), str(dst_path))

    # Hintergrund entfernen + transparente Raender abschneiden
    # rembg/ONNX-Inferenz ist CPU-bound → Threadpool damit der Event-Loop nicht blockiert.
    import asyncio as _asyncio
    final_path = await _asyncio.to_thread(postprocess_outfit_image, dst_path)
    final_filename = final_path.name

    # Altes Outfit-Bild loeschen
    old_image = outfit_obj.get("image", "")
    if old_image and old_image != final_filename:
        old_path = outfits_dir / old_image
        if old_path.exists():
            old_path.unlink()

    # Outfit-Image-Feld + Metadaten aktualisieren
    # Vollstaendige Metadaten aus der Generierung uebernehmen (Spec 1.2)
    _gen_meta = getattr(image_skill, 'last_image_meta', {}) or {}
    _image_meta = {
        "provider": _gen_meta.get("backend_type", ""),
        "service": _gen_meta.get("backend", ""),
        "model": _gen_meta.get("model", ""),
        "loras": _gen_meta.get("loras", loras_override or []),
        "prompt": prompt_text,
        "negative_prompt": _gen_meta.get("negative_prompt", ""),
        "seed": _gen_meta.get("seed", 0),
        "width": outfit_w or _gen_meta.get("width", 0),
        "height": outfit_h or _gen_meta.get("height", 0),
        "created_at": _gen_meta.get("created_at", ""),
        "duration_s": _gen_meta.get("duration_s", 0),
        "reference_images": _gen_meta.get("reference_images", {}),
        "workflow": _gen_meta.get("workflow", workflow_name),
        "model_override": model_override,
        "faceswap": _gen_meta.get("faceswap", False),
    }
    # Bild verknuepfen (loescht automatisch alte Variants).
    # update_outfit_image schreibt die Sidecar-JSON neben dem PNG (Spec 1.2).
    update_outfit_image(character_name, outfit_id, final_filename, image_meta=_image_meta)

    image_url = f"/characters/{character_name}/outfits/{final_filename}"
    return {"status": "success", "image": final_filename, "image_url": image_url}


@router.post("/{character_name}/outfits/generate-all-images")
async def generate_all_outfit_images_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Generiert Bilder fuer alle Outfits eines Characters (Bulk, im Hintergrund).

    Verwendet die gleiche Pipeline wie generate_outfit_image_route,
    aber fuer jedes Outfit einzeln. Laeuft im Hintergrund ueber die Task-Queue.
    """
    import os
    import json as _json
    import threading
    data = await request.json()
    user_id = data.get("user_id", "")

    outfits = get_character_outfits(character_name)
    if not outfits:
        return {"status": "error", "detail": "Keine Outfits vorhanden"}

    # Einstellungen aus Dialog (ohne Prompt — wird pro Outfit berechnet)
    workflow_name = data.get("workflow", "").strip()
    backend_name = data.get("backend", "").strip()
    loras_override = data.get("loras")
    model_override = data.get("model_override", "").strip()

    # Outfits mit Beschreibung filtern
    eligible = [o for o in outfits if o.get("outfit", "").strip()]
    if not eligible:
        return {"status": "error", "detail": "Keine Outfits mit Beschreibung vorhanden"}

    logger.info("Bulk Outfit-Bild Generierung: %s, %d Outfits", character_name, len(eligible))

    def _generate_all():
        from app.core.task_queue import get_task_queue
        from app.models.character_template import resolve_profile_tokens, get_template
        from app.models.character import (
            get_character_outfits_dir, postprocess_outfit_image, update_outfit_image)
        import shutil
        import re

        _tq = get_task_queue()
        _track_id = _tq.track_start(
            "bulk_outfit_images", f"Outfit-Bilder ({len(eligible)})",
            agent_name=character_name)

        profile = get_character_profile(character_name)
        tmpl = get_template(profile.get("template", "")) if profile.get("template") else None

        skill_manager = get_skill_manager()
        image_skill = skill_manager.get_skill("image_generation")
        if not image_skill:
            _tq.track_finish(_track_id, error="ImageGenerationSkill nicht verfuegbar")
            return

        success_count = 0
        for idx, outfit_obj in enumerate(eligible, 1):
            outfit_id = outfit_obj.get("id", "")
            outfit_name = outfit_obj.get("name", outfit_id[:8])
            outfit_description = outfit_obj.get("outfit", "")

            _tq.track_update_label(_track_id, f"Outfit {idx}/{len(eligible)}: {outfit_name}")
            logger.info("Bulk Outfit %d/%d: %s (%s)", idx, len(eligible), outfit_name, outfit_id[:8])

            # Tokens aufloesen
            if outfit_description and "{" in outfit_description:
                outfit_description = resolve_profile_tokens(
                    outfit_description, profile, template=tmpl, target_key="outfit")

            # Prompt via Hilfsfunktion
            prompt_text = _build_outfit_image_prompt(character_name, outfit_description)

            # Appearance fuer Reference-Bilder
            appearance = get_character_appearance(character_name)
            if appearance and "{" in appearance:
                appearance = resolve_profile_tokens(
                    appearance, profile, template=tmpl, target_key="character_appearance")

            payload = {
                "prompt": prompt_text,
                "agent_name": character_name,
                "user_id": "",
                "auto_enhance": False,
                "force_faceswap": True,
                "skip_gallery": True,
                "appearances": [{"name": character_name, "appearance": appearance or ""}],
                "profile_only": True,
            }
            if workflow_name:
                payload["workflow"] = workflow_name
            if backend_name:
                payload["backend"] = backend_name
            if loras_override is not None:
                payload["loras"] = loras_override
            if model_override:
                payload["model_override"] = model_override

            try:
                result = image_skill.execute(_json.dumps(payload))
                match = re.search(r'/characters/[^/]+/images/([^?)\n]+)', result)
                if not match:
                    logger.warning("Bulk Outfit %s: Kein Bild im Ergebnis", outfit_name)
                    continue

                image_filename = match.group(1)
                images_dir = get_character_images_dir(character_name)
                outfits_dir = get_character_outfits_dir(character_name)
                src_path = images_dir / image_filename
                dst_path = outfits_dir / image_filename
                if src_path.exists():
                    shutil.move(str(src_path), str(dst_path))

                final_path = postprocess_outfit_image(dst_path)
                final_filename = final_path.name

                # Altes Bild loeschen
                old_image = outfit_obj.get("image", "")
                if old_image and old_image != final_filename:
                    old_path = outfits_dir / old_image
                    if old_path.exists():
                        old_path.unlink()

                # Metadaten
                _gen_meta = getattr(image_skill, 'last_image_meta', {}) or {}
                _image_meta = {
                    "provider": _gen_meta.get("backend_type", ""),
                    "service": _gen_meta.get("backend", ""),
                    "model": _gen_meta.get("model", ""),
                    "loras": _gen_meta.get("loras", []),
                    "prompt": prompt_text,
                    "negative_prompt": _gen_meta.get("negative_prompt", ""),
                    "seed": _gen_meta.get("seed", 0),
                    "workflow": _gen_meta.get("workflow", workflow_name),
                    "model_override": model_override,
                    "faceswap": _gen_meta.get("faceswap", False),
                }
                # update_outfit_image schreibt PNG-Verknuepfung + Sidecar-JSON
                update_outfit_image(character_name, outfit_id, final_filename, image_meta=_image_meta)

                success_count += 1
                logger.info("Bulk Outfit %s: Bild generiert -> %s", outfit_name, final_filename)

            except Exception as e:
                logger.error("Bulk Outfit %s fehlgeschlagen: %s", outfit_name, e)

        _tq.track_finish(_track_id)
        logger.info("Bulk Outfit-Bilder fertig: %d/%d erfolgreich", success_count, len(eligible))

    threading.Thread(target=_generate_all, daemon=True, name=f"bulk-outfit-{character_name}").start()
    return {"status": "started", "count": len(eligible)}


@router.get("/{character_name}/current-outfit")
def get_current_outfit_route(character_name: str) -> Dict[str, Any]:
    """Gibt das aktuelle Outfit basierend auf Location und Activity zurueck"""
    from app.models.world import get_location_name as _get_loc_name
    from app.models.character import get_character_current_room
    from app.models.character import build_equipped_outfit_prompt
    outfit = (build_equipped_outfit_prompt(character_name) or "").removeprefix("wearing: ")
    current_location_id = get_character_current_location(character_name)
    current_activity = get_character_current_activity(character_name)
    current_room = get_character_current_room(character_name)
    return {
        "character": character_name,
        "current_outfit_description": outfit or "",
        "current_location": _get_loc_name(current_location_id) if current_location_id else "",
        "current_location_id": current_location_id or "",
        "current_activity": current_activity or "",
        "current_room": current_room or "",
    }


@router.post("/{character_name}/current-outfit/refresh")
async def refresh_current_outfit(character_name: str, request: Request) -> Dict[str, Any]:
    """Outfit-Compliance pruefen und Pieces nach Dress-Code tauschen."""
    from app.models.inventory import apply_outfit_type_compliance
    from app.models.character import get_character_profile, build_equipped_outfit_prompt
    from app.models.world import get_location
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    profile = get_character_profile(character_name) or {}
    loc_id = profile.get("current_location", "")
    room_id = profile.get("current_room", "")
    target_type = ""
    if loc_id:
        loc = get_location(loc_id) or {}
        if room_id:
            for r in (loc.get("rooms") or []):
                if r.get("id") == room_id:
                    target_type = (r.get("outfit_type") or "").strip()
                    break
        if not target_type:
            target_type = (loc.get("outfit_type") or "").strip()
    result = {}
    if target_type:
        result = apply_outfit_type_compliance(character_name, target_type)
    outfit_text = build_equipped_outfit_prompt(character_name)
    return {"character": character_name, "current_outfit_description": outfit_text, "compliance": result}




@router.get("/{character_name}/outfit-lock")
def get_outfit_lock_route(character_name: str) -> Dict[str, Any]:
    """Gibt den Sperrstatus des Outfits zurueck."""
    from app.models.character import is_outfit_locked
    return {"character": character_name, "locked": is_outfit_locked(character_name)}


@router.post("/{character_name}/outfit-lock")
async def set_outfit_lock_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Setzt/entfernt die Outfit-Sperre (blockiert Auto-Outfit-Aenderungen)."""
    from app.models.character import set_outfit_locked, is_outfit_locked
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    locked = bool(data.get("locked"))
    set_outfit_locked(character_name, locked)
    return {"character": character_name, "locked": is_outfit_locked(character_name)}


@router.get("/{character_name}/default-outfit")
def get_default_outfit_route(character_name: str) -> Dict[str, Any]:
    """Gibt das Default-Outfit zurueck"""
    from app.models.character import get_character_default_outfit
    outfit = get_character_default_outfit(character_name)
    return {"character": character_name, "default_outfit": outfit or ""}


@router.post("/{character_name}/default-outfit")
async def update_default_outfit(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert das Default-Outfit"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        outfit = data.get("default_outfit", "")

        from app.models.character import save_character_default_outfit
        save_character_default_outfit(character_name, outfit)
        return {"status": "success", "character": character_name, "default_outfit": outfit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Expression Image (mood + pose variants) ---

@router.get("/{character_name}/outfit-expression")
def get_outfit_expression(character_name: str, mood: str = "", activity: str = "",
                          pieces: str = "", items: str = "",
                          piece_colors: str = "",
                          override: int = 0, trigger: int = 0, force: int = 0,
                          fallback: str = ""):
    """Returns the expression/pose variant for current mood + activity + equipped.

    Variant = Character-Appearance + angelegte Items (Pieces + Equipped-Items) +
    Pose (aus activity, Default wenn leer) + Expression (aus mood, Default wenn
    leer). Cache-Key haengt NICHT mehr von einem Outfit-Objekt ab — es gibt
    kein Basis-Outfitbild mehr.

    Query params:
        pieces: Optional "slot1:itemId1,slot2:itemId2" — ueberschreibt den
                real-equipped Piece-State (z.B. Wardrobe-Preview eines Sets
                das noch nicht angezogen ist).
        items: Optional "id1,id2" — ueberschreibt equipped_items.
        override: 1 erzwingt Verwendung von pieces/items auch wenn leer
                  (= "leerer Equipped-State"); ohne override werden leere
                  pieces/items als "nicht mitgegeben" interpretiert und der
                  Real-State aus dem Profil geladen.
        fallback: "default" — wenn keine Variant im Cache (aber Generierung
                  laeuft), wird das Profilbild als Platzhalter zurueckgegeben
                  (200 statt 202). Frontend zeigt damit ein sinnvolles Bild
                  bis der echte Variant fertig ist.

    Antworten: 200 mit Bild, 202 wenn Generierung laeuft, 404 wenn fehl-
    geschlagen / kein Generator verfuegbar.
    """
    from app.core.expression_regen import (
        get_cached_expression, trigger_expression_generation, is_generating, has_failed)

    # Mood/Activity: Wenn nicht explizit uebergeben, Character-State als Default
    # nehmen — damit Frontend den zu Chat/Scheduler passenden Variant-Cache
    # findet. Override-Modus laesst Leer-Params zu (Set-Vorschau ist generisch).
    _is_override = bool(override or pieces or items or piece_colors)
    if not _is_override:
        if not mood:
            try:
                from app.models.character import get_character_current_feeling
                mood = get_character_current_feeling(character_name) or ""
            except Exception:
                mood = ""
        if not activity:
            try:
                from app.models.character import get_character_current_activity
                activity = get_character_current_activity(character_name) or ""
            except Exception:
                activity = ""

    # Equipped-State: Override-Params haben Prioritaet, sonst Real-State aus Profil.
    # Override-Modus ist read-only: es wird nur im Cache gesucht, keine Generierung
    # getriggert — Vorschau-Flows (z.B. Set-Durchschalten in der Garderobe) sollen
    # nicht massenweise Generierungen anstossen.
    _eq_pieces: Optional[Dict[str, str]] = None
    _eq_items: Optional[List[str]] = None
    _eq_meta: Optional[Dict[str, Dict[str, Any]]] = None
    if _is_override:
        _eq_pieces = {}
        for pair in pieces.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            slot, iid = pair.split(":", 1)
            slot, iid = slot.strip(), iid.strip()
            if slot and iid:
                _eq_pieces[slot] = iid
        _eq_items = [s.strip() for s in items.split(",") if s.strip()]
        # piece_colors: "slot:color,slot:color" — nur fuer Slots die auch in pieces sind
        _eq_meta = {}
        for pair in piece_colors.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            slot, color = pair.split(":", 1)
            slot, color = slot.strip(), color.strip()
            if slot and color and slot in _eq_pieces:
                _eq_meta[slot] = {"color": color}
    else:
        try:
            from app.models.inventory import get_equipped_pieces, get_equipped_items
            from app.models.character import get_character_profile
            _eq_pieces = get_equipped_pieces(character_name)
            _eq_items = get_equipped_items(character_name)
            _profile = get_character_profile(character_name) or {}
            _eq_meta = _profile.get("equipped_pieces_meta") or {}
        except Exception:
            _eq_pieces, _eq_items, _eq_meta = None, None, None

    # Check cache
    cached = get_cached_expression(character_name, mood, activity,
                                    equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                                    equipped_pieces_meta=_eq_meta)
    if cached and force:
        # Force-Regenerate: gecachtes PNG + Sidecar loeschen, damit der
        # folgende Trigger-Pfad die Variant neu erzeugt.
        try:
            cached.unlink()
            _sidecar = cached.with_suffix(".json")
            if _sidecar.exists():
                _sidecar.unlink()
            logger.info("Force-Regenerate Expression: %s", cached.name)
        except OSError as e:
            logger.warning("Force-Regenerate konnte Cache nicht loeschen: %s", e)
        cached = None
    if cached:
        media_type = mimetypes.guess_type(str(cached))[0] or "image/png"
        return FileResponse(
            path=str(cached),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"})

    # Helper: Fallback-Bild wenn die gesuchte Variant noch nicht bereit ist.
    # Profilbild ist NIE eine Option — Profilbild = Roster-Avatar, nicht Scene.
    # Reihenfolge:
    #   1) Default-Variant (mood="" + activity="") mit aktuellem Equipped-State.
    #   2) Neuestes beliebiges Expression-Variant des Characters.
    # Wenn beides fehlt: None → Aufrufer liefert 202 (Generierung laeuft) und
    # FE pollt weiter bis das echte Variant kommt.
    def _serve_fallback():
        from app.core.expression_regen import _get_expressions_dir, _safe_name

        def _serve_file(path, status):
            media_type = mimetypes.guess_type(str(path))[0] or "image/png"
            return FileResponse(
                path=str(path),
                media_type=media_type,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Variant-Status": status,
                })

        # 1) Default-Variant: gleicher Equipped-State, mood+activity leer
        try:
            default_cached = get_cached_expression(
                character_name, "", "",
                equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                equipped_pieces_meta=_eq_meta,
            )
        except Exception:
            default_cached = None
        if default_cached and default_cached.exists():
            return _serve_file(default_cached, "fallback-default")

        # 2) Neuestes beliebiges Expression-Variant des Characters
        try:
            expr_dir = _get_expressions_dir(character_name)
            prefix = _safe_name(character_name)
            candidates = []
            if expr_dir.exists():
                for ext in ("*.png", "*.jpg", "*.webp"):
                    candidates.extend(expr_dir.glob(f"{prefix}_{ext}"))
            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                return _serve_file(newest, "fallback-variant-any")
        except Exception:
            pass

        # KEIN Profilbild-Fallback — lieber nichts als das falsche Bild.
        return None

    _want_fallback = (fallback or "").strip().lower() == "default"

    # Override-Modus: per default nur Cache lesen. Mit trigger=1 wird die
    # Generierung fuer genau diese Equipped-Kombination explizit angestossen
    # (z.B. Wardrobe-Vorschau-Button, der fuer das ausgewaehlte Set die
    # Variant vorberechnen soll ohne vorher anzuziehen).
    if _is_override and not trigger:
        if is_generating(character_name, mood, activity,
                         equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                         equipped_pieces_meta=_eq_meta):
            if _want_fallback:
                fb = _serve_fallback()
                if fb is not None:
                    return fb
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=202,
                content={"status": "generating", "mood": mood, "activity": activity})
        raise HTTPException(status_code=404, detail="Keine Variant im Cache")

    if has_failed(character_name, mood, activity,
                  equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                  equipped_pieces_meta=_eq_meta):
        if force or trigger:
            # Force/Trigger: failed-Marker loeschen damit ein Retry moeglich ist
            from app.core.expression_regen import clear_failed_marker
            clear_failed_marker(character_name, mood, activity,
                                equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                                equipped_pieces_meta=_eq_meta)
        else:
            raise HTTPException(status_code=404, detail="Variant-Generierung fehlgeschlagen")

    if is_generating(character_name, mood, activity,
                     equipped_pieces=_eq_pieces, equipped_items=_eq_items):
        if _want_fallback:
            fb = _serve_fallback()
            if fb is not None:
                return fb
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=202,
            content={"status": "generating", "mood": mood, "activity": activity})

    # Prefix uebernimmt trigger_expression_generation per Default aus dem Env
    # (OUTFIT_IMAGE_PROMPT_PREFIX) — Auto-Regen + Vorschau identisch.
    # Cooldown wird umgangen wenn kein Cache existiert (sonst zeigt die Scene nie
    # ein Variant-Bild), oder wenn explizit per trigger=1 angefordert.
    # Explizit-Trigger (trigger=1, Garderobe-Preview-Button): kein Debounce,
    # User erwartet sofortige Generierung. Auto-Pfade (cache-miss beim Render)
    # coalescen, damit ein Chat-Turn nicht 3 Varianten erzeugt.
    started = trigger_expression_generation(character_name, mood, activity,
                                             equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                                             equipped_pieces_meta=_eq_meta,
                                             ignore_cooldown=True,
                                             ignore_feature_gate=bool(trigger),
                                             coalesce=not bool(trigger))
    if started:
        if _want_fallback:
            fb = _serve_fallback()
            if fb is not None:
                return fb
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=202,
            content={"status": "generating", "mood": mood, "activity": activity})
    # Konkurrierender Request: andere Anfrage hat gerade schon gestartet → 202 statt 404
    if is_generating(character_name, mood, activity,
                     equipped_pieces=_eq_pieces, equipped_items=_eq_items,
                     equipped_pieces_meta=_eq_meta):
        if _want_fallback:
            fb = _serve_fallback()
            if fb is not None:
                return fb
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=202,
            content={"status": "generating", "mood": mood, "activity": activity})
    raise HTTPException(status_code=404, detail="Keine Variant verfuegbar")


@router.delete("/{character_name}/outfit-expression/cache")
async def clear_expression_cache_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Clears the expression image cache for a character."""
    from app.core.expression_regen import clear_expression_cache
    data = await request.json()
    user_id = data.get("user_id", "")
    count = clear_expression_cache(character_name)
    return {"status": "success", "cleared": count}


@router.get("/{character_name}/outfit-imagegen")
async def get_outfit_imagegen_route(character_name: str) -> Dict[str, Any]:
    """Liefert die per-Character Overrides fuer den Outfit-/Variant-Image-
    Service (Workflow + Model + LoRAs). Leere Werte = Defaults."""
    from app.models.character import get_character_profile
    prof = get_character_profile(character_name) or {}
    override = prof.get("outfit_imagegen") or {}
    if not isinstance(override, dict):
        override = {}
    return {
        "workflow": override.get("workflow", "") or "",
        "model": override.get("model", "") or "",
        "loras": override.get("loras", []) or [],
    }


@router.put("/{character_name}/outfit-imagegen")
async def set_outfit_imagegen_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert Workflow/Model/LoRA-Override fuer den Outfit-Image-Service.
    Alle Felder leer loescht den Override komplett."""
    from app.models.character import get_character_profile, save_character_profile
    body = await request.json()
    workflow = (body.get("workflow") or "").strip()
    model = (body.get("model") or "").strip()
    loras = body.get("loras") or []
    if not isinstance(loras, list):
        loras = []
    clean_loras = []
    for l in loras:
        if not isinstance(l, dict):
            continue
        nm = (l.get("name") or "").strip()
        if not nm or nm == "None":
            continue
        try:
            st = float(l.get("strength", 1.0))
        except Exception:
            st = 1.0
        clean_loras.append({"name": nm, "strength": st})
    prof = get_character_profile(character_name) or {}
    if workflow or model or clean_loras:
        prof["outfit_imagegen"] = {
            "workflow": workflow,
            "model": model,
            "loras": clean_loras,
        }
    elif "outfit_imagegen" in prof:
        del prof["outfit_imagegen"]
    save_character_profile(character_name, prof)
    return {"status": "ok", "workflow": workflow, "model": model, "loras": clean_loras}


@router.get("/{character_name}/slot-overrides")
async def get_slot_overrides_route(character_name: str) -> Dict[str, Any]:
    """Liefert per-Slot Prompt+LoRA-Overrides (9 Slots).

    Struktur: {slot: {prompt: str, lora: {name, strength}}}.
    Greifen nur wenn der Slot leer und nicht verdeckt ist.
    """
    from app.models.character import get_character_profile
    from app.models.inventory import VALID_PIECE_SLOTS
    prof = get_character_profile(character_name) or {}
    raw = prof.get("slot_overrides") or {}
    if not isinstance(raw, dict):
        raw = {}
    out: Dict[str, Any] = {}
    for slot in VALID_PIECE_SLOTS:
        entry = raw.get(slot) or {}
        if not isinstance(entry, dict):
            entry = {}
        lora = entry.get("lora") or {}
        if not isinstance(lora, dict):
            lora = {}
        out[slot] = {
            "prompt": (entry.get("prompt") or "").strip(),
            "lora": {
                "name": (lora.get("name") or "").strip(),
                "strength": float(lora.get("strength", 1.0) or 1.0),
            },
        }
    return {"slots": out, "order": list(VALID_PIECE_SLOTS)}


@router.put("/{character_name}/slot-overrides")
async def set_slot_overrides_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert per-Slot Prompt+LoRA-Overrides.

    Body: {slots: {slot: {prompt: str, lora: {name, strength}}}}.
    Leere Eintraege (kein Prompt + kein LoRA) werden entfernt.
    """
    from app.models.character import get_character_profile, save_character_profile
    from app.models.inventory import VALID_PIECE_SLOTS
    body = await request.json()
    slots_in = body.get("slots") or {}
    if not isinstance(slots_in, dict):
        slots_in = {}
    cleaned: Dict[str, Any] = {}
    for slot in VALID_PIECE_SLOTS:
        entry = slots_in.get(slot) or {}
        if not isinstance(entry, dict):
            continue
        prompt = (entry.get("prompt") or "").strip()
        lora_raw = entry.get("lora") or {}
        lora_name = ""
        lora_strength = 1.0
        if isinstance(lora_raw, dict):
            lora_name = (lora_raw.get("name") or "").strip()
            if lora_name.lower() == "none":
                lora_name = ""
            try:
                lora_strength = float(lora_raw.get("strength", 1.0))
            except Exception:
                lora_strength = 1.0
        if not prompt and not lora_name:
            continue
        out: Dict[str, Any] = {}
        if prompt:
            out["prompt"] = prompt
        if lora_name:
            out["lora"] = {"name": lora_name, "strength": lora_strength}
        cleaned[slot] = out
    prof = get_character_profile(character_name) or {}
    if cleaned:
        prof["slot_overrides"] = cleaned
    elif "slot_overrides" in prof:
        del prof["slot_overrides"]
    save_character_profile(character_name, prof)
    return {"status": "ok", "slots": cleaned}


# --- Profile (bulk read/update) ---

def _soul_field_keys(template_id: str) -> set:
    """Set der Profile-Keys deren Inhalt aus einer MD-Datei kommt (source_file)."""
    if not template_id:
        return set()
    try:
        from app.models.character_template import get_template
        tmpl = get_template(template_id)
    except Exception:
        return set()
    if not tmpl:
        return set()
    keys = set()
    for section in tmpl.get("sections", []):
        for field in section.get("fields", []):
            if field.get("source_file") and field.get("key"):
                keys.add(field["key"])
    return keys


@router.get("/{character_name}/profile")
def get_profile_route(character_name: str) -> Dict[str, Any]:
    """Gibt das vollstaendige Character-Profil zurueck"""
    profile = get_character_profile(character_name)

    # Token-aufgeloeste Varianten der Appearance-Felder beilegen, damit das
    # Frontend (z.B. Profilbild-Generierung) den fertigen Text bekommt ohne
    # selbst Tokens parsen zu muessen.
    try:
        from app.models.character_template import (
            get_template, resolve_profile_tokens)
        _tmpl_id = profile.get("template", "") or ""
        _tmpl = get_template(_tmpl_id) if _tmpl_id else None
        for _key in ("character_appearance", "face_appearance"):
            _raw = (profile.get(_key) or "").strip()
            if not _raw or "{" not in _raw:
                continue
            _resolved = resolve_profile_tokens(
                _raw, profile, template=_tmpl, target_key="character_appearance")
            if _resolved and _resolved != _raw:
                if not isinstance(profile, dict):
                    profile = dict(profile)
                profile[f"{_key}_resolved"] = _resolved
    except Exception:
        pass

    # Ortsname auflösen damit der Editor den Namen zeigt statt der ID
    loc_id = profile.get("current_location", "")
    if loc_id:
        try:
            from app.models.world import get_location_name as _get_loc_name
            resolved = _get_loc_name(loc_id)
            if resolved:
                profile = dict(profile)
                profile["current_location"] = resolved
        except Exception:
            pass
    # Raumname auflösen (Room-ID → Name)
    room_id = profile.get("current_room", "")
    if room_id and loc_id:
        try:
            from app.models.world import get_location, get_room_by_id
            loc_data = get_location(loc_id)
            if loc_data:
                room = get_room_by_id(loc_data, room_id)
                if room and room.get("name"):
                    if not isinstance(profile, dict):
                        profile = dict(profile)
                    profile["current_room"] = room["name"]
        except Exception:
            pass
    return {"character": character_name, "profile": profile}


@router.get("/{character_name}/active-conditions")
def get_active_conditions_route(character_name: str) -> Dict[str, Any]:
    """Gibt aktive Conditions mit Icon/Label/Restdauer zurueck.

    Abgelaufene Conditions werden gefiltert. Icons/Labels kommen aus den
    Prompt-Filtern (Game Admin → Zustaende).
    """
    from datetime import datetime as _dt
    from app.core.prompt_filters import load_filters

    profile = get_character_profile(character_name)
    active = profile.get("active_conditions", []) or []

    # Index: condition_name (lowercased) -> {icon, label, image_modifier}.
    # Filter-`id` ist der kanonische Condition-Name (neues Modell): wenn der
    # Tag in active_conditions auftaucht, triggert der Filter implizit. Wir
    # bauen den Lookup primaer ueber id; legacy-Filter mit `condition:<name>`-
    # Expression werden zusaetzlich als Alias indexiert, damit alte Daten
    # weiter ein Icon bekommen.
    meta_by_name: Dict[str, Dict[str, str]] = {}
    for f in load_filters():
        meta = {
            "icon": f.get("icon", "") or "",
            "label": f.get("label", "") or "",
            "image_modifier": f.get("image_modifier", "") or "",
        }
        fid = (f.get("id") or "").strip().lower()
        if fid:
            meta_by_name.setdefault(fid, dict(meta, label=meta["label"] or fid))
        cond_str = (f.get("condition") or "").strip()
        if cond_str.lower().startswith("condition:"):
            name = cond_str[10:].strip().lower()
            if name:
                meta_by_name.setdefault(name, dict(meta, label=meta["label"] or name))

    now = _dt.now()
    result = []
    for cond in active:
        name = (cond.get("name") or "").strip()
        if not name:
            continue
        # Abgelaufen?
        duration_h = cond.get("duration_hours", 0) or 0
        remaining_h = None
        if duration_h:
            try:
                started = _dt.fromisoformat(cond["started_at"])
                elapsed_s = (now - started).total_seconds()
                total_s = duration_h * 3600
                if elapsed_s > total_s:
                    continue
                remaining_h = round((total_s - elapsed_s) / 3600, 1)
            except (ValueError, KeyError):
                pass
        meta = meta_by_name.get(name.lower(), {})
        result.append({
            "name": name,
            "label": meta.get("label") or name,
            "icon": meta.get("icon", ""),
            "remaining_hours": remaining_h,
            "source": cond.get("source", ""),
        })
    return {"character": character_name, "conditions": result}


@router.post("/{character_name}/profile")
async def update_profile_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert Character-Profil Felder (bulk update)"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        fields = data.get("fields", {})
        if not fields:
            raise HTTPException(status_code=400, detail="fields fehlt")

        profile = get_character_profile(character_name)

        # current_location: Name zurueck in ID aufloesen, damit die
        # Weltkarte den Character weiterhin findet (GET liefert den
        # aufgeloesten Namen, POST bekommt ihn zurueck).
        if "current_location" in fields:
            loc_val = fields["current_location"]
            if loc_val:
                from app.models.world import resolve_location, get_location_id
                loc_id = get_location_id(loc_val)
                if loc_id:
                    fields["current_location"] = loc_id
                else:
                    loc_obj = resolve_location(loc_val)
                    if loc_obj and loc_obj.get("id"):
                        fields["current_location"] = loc_obj["id"]

        # Filter out __custom__ sentinel values (UI placeholder for custom input)
        for k, v in list(fields.items()):
            if v == "__custom__":
                fields[k] = ""

        # Felder mit source_file gehoeren in MD-Files, NICHT ins JSON-Profil.
        # Falls jemand sie hier reinschickt, ignorieren — der Soul-Editor ist
        # zustaendig (siehe /characters/{char}/soul/*).
        _sf_keys = _soul_field_keys(profile.get("template", ""))
        for k in list(fields.keys()):
            if k in _sf_keys:
                fields.pop(k, None)

        profile.update(fields)
        save_character_profile(character_name, profile)
        return {"status": "success", "character": character_name,
                "updated_fields": list(fields.keys())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Config (character_config.json) ---

@router.get("/{character_name}/config")
def get_config_route(character_name: str) -> Dict[str, Any]:
    """Gibt die Character-Config zurueck (TTS, Extraction, etc.)"""
    config = get_character_config(character_name)
    return {"character": character_name, "config": config}


@router.post("/{character_name}/config")
async def update_config_route(character_name: str, request: Request) -> Dict[str, Any]:
    """Aktualisiert Character-Config Felder (bulk update)"""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        fields = data.get("fields", {})
        if not fields:
            raise HTTPException(status_code=400, detail="fields fehlt")

        config = get_character_config(character_name)
        config.update(fields)
        save_character_config(character_name, config)

        return {"status": "success", "character": character_name, "updated_fields": list(fields.keys())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Images ---

@router.post("/{character_name}/images")
async def upload_character_image(character_name: str, request: Request) -> Dict[str, Any]:
    """Laedt ein Bild hoch"""
    try:
        form = await request.form()
        file = form.get("file")

        if not file:
            raise HTTPException(status_code=400, detail="Keine Datei hochgeladen")

        allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp", "mp4"}
        filename = file.filename.lower()
        if not any(filename.endswith(ext) for ext in allowed_extensions):
            raise HTTPException(status_code=400, detail="Format nicht unterstuetzt")

        images_dir = get_character_images_dir(character_name)

        timestamp = int(time.time())
        file_ext = Path(filename).suffix
        image_filename = f"{character_name}_{timestamp}{file_ext}"
        image_path = images_dir / image_filename

        contents = await file.read()
        image_path.write_bytes(contents)

        add_character_image(character_name, image_filename)

        return {
            "status": "success",
            "filename": image_filename,
            "url": f"/characters/{character_name}/images/{image_filename}"
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/images")
def get_character_images_list(character_name: str) -> Dict[str, Any]:
    """Gibt Liste aller Bilder zurueck"""
    try:
        images = get_character_images(character_name)
        profile_image = get_character_profile_image(character_name)
        comments = get_character_image_comments(character_name)
        prompts = get_character_image_prompts(character_name)
        metadata = get_character_image_metadata(character_name)

        # Pruefen welche Bilder ein zugehoeriges Video haben ({stem}.mp4)
        images_dir = get_character_images_dir(character_name)
        image_videos = {}
        for img_name in images:
            video_path = images_dir / (Path(img_name).stem + ".mp4")
            if video_path.exists():
                image_videos[img_name] = video_path.name

        return {
            "character": character_name,
            "images": images,
            "profile_image": profile_image,
            "urls": [f"/characters/{character_name}/images/{img}" for img in images],
            "image_comments": comments,
            "image_prompts": prompts,
            "image_metadata": metadata,
            "image_videos": image_videos,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/images/profile")
def get_character_profile_image_file(character_name: str):
    """Serves the character's profile image directly (shortcut for notifications/avatars)."""
    from fastapi.responses import Response
    try:
        profile_img = get_character_profile_image(character_name)
        if not profile_img:
            return Response(status_code=404)
        images_dir = get_character_images_dir(character_name)
        image_path = images_dir / profile_img
        if not image_path.exists():
            return Response(status_code=404)
        media_type, _ = mimetypes.guess_type(str(image_path))
        return FileResponse(
            image_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=300"}
        )
    except Exception:
        return Response(status_code=404)


@router.get("/{character_name}/images/{image_filename}")
def get_character_image(character_name: str, image_filename: str):
    """Ruft ein spezifisches Bild ab"""
    from fastapi.responses import Response
    try:
        if ".." in image_filename or "/" in image_filename:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        images_dir = get_character_images_dir(character_name)
        image_path = images_dir / image_filename

        if not image_path.exists():
            return Response(
                status_code=404,
                headers={"Cache-Control": "public, max-age=300"}
            )

        media_type, _ = mimetypes.guess_type(str(image_path))
        return FileResponse(
            image_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-cache"}
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/outfits/{image_filename}")
def get_character_outfit_image(character_name: str, image_filename: str):
    """Liefert ein Outfit-Referenzbild aus dem outfits/ Verzeichnis."""
    from fastapi.responses import Response
    try:
        if ".." in image_filename or "/" in image_filename:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        from app.models.character import get_character_outfits_dir
        outfits_dir = get_character_outfits_dir(character_name)
        image_path = outfits_dir / image_filename

        if not image_path.exists():
            return Response(status_code=404)

        media_type, _ = mimetypes.guess_type(str(image_path))
        return FileResponse(
            image_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-cache"}
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/images/{image_filename}/comment")
async def save_image_comment_endpoint(character_name: str, image_filename: str, request: Request) -> Dict[str, Any]:
    """Speichert einen Kommentar fuer ein Bild"""
    try:
        body = await request.json()
        comment = body.get("comment", "")
        add_character_image_comment(character_name, image_filename, comment)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/profile-image/{image_filename}")
def set_character_profile_image_endpoint(character_name: str, image_filename: str) -> Dict[str, Any]:
    """Setzt das Profilbild"""
    try:
        if set_character_profile_image(character_name, image_filename):
            return {
                "status": "success",
                "character": character_name,
                "profile_image": image_filename
            }
        else:
            raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{character_name}/images/{image_filename}")
def delete_character_image_endpoint(character_name: str, image_filename: str) -> Dict[str, Any]:
    """Loescht ein Bild"""
    try:
        if ".." in image_filename or "/" in image_filename:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        if delete_character_image(character_name, image_filename):
            return {
                "status": "success",
                "character": character_name,
                "deleted_image": image_filename
            }
        else:
            raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{character_name}/images/{image_filename}/animation")
def delete_image_animation(character_name: str, image_filename: str) -> Dict[str, Any]:
    """Loescht nur die Animation (Video) eines Bildes, nicht das Bild selbst."""
    if ".." in image_filename or "/" in image_filename:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    images_dir = get_character_images_dir(character_name)
    stem = Path(image_filename).stem
    video_path = images_dir / f"{stem}.mp4"

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Keine Animation vorhanden")

    video_path.unlink()
    logger.info("Animation geloescht: %s", video_path.name)

    # animate_prompt und animate_created_at aus Metadaten entfernen
    from app.models.character import _load_single_image_meta, _save_single_image_meta
    meta = _load_single_image_meta(character_name, image_filename)
    changed = False
    for key in ("animate_prompt", "animate_created_at"):
        if key in meta:
            del meta[key]
            changed = True
    if changed:
        _save_single_image_meta(character_name, image_filename, meta)

    return {"status": "success", "deleted_video": f"{stem}.mp4"}


@router.post("/{character_name}/cleanup-images")
def cleanup_images_endpoint(character_name: str) -> Dict[str, Any]:
    """Loescht verwaiste Bilddateien die nicht im Profil registriert sind."""
    try:
        return cleanup_orphaned_images(character_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Reset Endpoints ---

@router.post("/reset/user/{user_id}")
def reset_user_profile() -> Dict[str, str]:
    """Setzt das komplette User-Profil zurueck"""
    try:
        import json
        from app.core.paths import get_storage_dir
        profile_path = get_storage_dir()

        for file in profile_path.glob("*.json"):
            if "_chat_" in file.name:
                continue
            try:
                data = json.loads(file.read_text())
                if isinstance(data, dict) and "_user_id" in data:
                    file.unlink()
                    break
            except:
                pass

        return {"status": "success", "message": "User profile reset"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset/character/{user_id}")
def reset_character_selection() -> Dict[str, str]:
    """Setzt die Character-Auswahl zurueck"""
    try:
        set_current_character("")
        return {"status": "success", "message": "Character selection reset"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Skills Management ---

@router.post("/skills/reload")
def reload_skills() -> Dict[str, Any]:
    """Laedt alle Skills neu ohne Server-Neustart."""
    try:
        from app.skills.animate import reload_animate_services
        reload_animate_services()
        result = reload_skill_manager()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skills/list")
def list_skills() -> Dict[str, Any]:
    """Listet alle aktuell geladenen Skills auf"""
    try:
        skill_manager = get_skill_manager()
        return {
            "skills": skill_manager.get_skill_info(),
            "count": len(skill_manager.skills)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{character_name}/skills/available")
def get_available_skills_for_character(character_name: str) -> Dict[str, Any]:
    """Returns all globally loaded skills with per-character enabled state and config fields."""
    try:
        skill_manager = get_skill_manager()
        skills = []
        for skill in skill_manager.skills:
            skill_id = skill.SKILL_ID
            if not skill_id:
                continue
            config = get_character_skill_config(character_name, skill_id)
            # Default: ALWAYS_LOAD Skills starten deaktiviert, andere aktiviert
            default_enabled = not getattr(skill, 'ALWAYS_LOAD', False)
            enabled = default_enabled
            if config and "enabled" in config:
                enabled = bool(config["enabled"])

            # Config fields with defaults, types, and current values
            config_fields = skill.get_config_fields()
            if config_fields:
                for field_name, field_info in config_fields.items():
                    if config and field_name in config:
                        field_info["value"] = config[field_name]
                    else:
                        field_info["value"] = field_info["default"]

            skills.append({
                "skill_id": skill_id,
                "name": skill.name,
                "description": skill.description,
                "enabled": enabled,
                "config_fields": config_fields if config_fields else None,
            })

        # Location-Liste fuer Skill-Config-Felder vom Typ "locations"
        from app.models.world import list_locations
        all_locations = [{"id": loc.get("id", ""), "name": loc.get("name", "")}
                         for loc in list_locations() if loc.get("id")]

        return {"skills": skills, "locations": all_locations}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Soul Editor — MD-Files unter characters/{Char}/soul/
# ---------------------------------------------------------------------------

# File-Default-Whitelist (single source of truth fuer Soul-Editor-UI)
from app.core.soul_sections import (
    EDITABLE_SECTIONS as _SOUL_EDITABLE,
    LOCKED_SECTIONS as _SOUL_LOCKED,
    SECTION_FILE_MAP as _SOUL_FILE_MAP,
    EDITABLE_MARKER as _SOUL_EDITABLE_MARKER)


def _parse_soul_sections(text: str) -> List[Dict[str, Any]]:
    """Zerlegt MD-Text in {heading, body, has_editable_marker} Sections.

    Top-`# Heading` wird als 'top' Section markiert. Body ist Roh-Inhalt
    OHNE den EDITABLE-Marker (UI rendert Status-Indikator separat).
    """
    sections = []
    cur_h = None
    cur_lvl = 0
    cur_body: List[str] = []

    def _flush():
        if cur_h is None and not cur_body:
            return
        body_lines = list(cur_body)
        # EDITABLE-Marker erkennen + aus body entfernen
        has_marker = any(_SOUL_EDITABLE_MARKER in ln for ln in body_lines)
        clean_body = [ln for ln in body_lines if _SOUL_EDITABLE_MARKER not in ln]
        body_text = "\n".join(clean_body).strip()
        sections.append({
            "level": cur_lvl,
            "heading": cur_h or "",
            "body": body_text,
            "editable_marker": has_marker,
        })

    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            _flush()
            cur_h = line[2:].strip()
            cur_lvl = 1
            cur_body = []
        elif line.startswith("## "):
            _flush()
            cur_h = line[3:].strip()
            cur_lvl = 2
            cur_body = []
        else:
            cur_body.append(line)
    _flush()
    return sections


def _soul_file_meta(section_id: str) -> Dict[str, Any]:
    """Gibt Meta zu einer Soul-Datei: file-default lock-status + path."""
    if section_id in _SOUL_EDITABLE:
        default = "editable"
    elif section_id in _SOUL_LOCKED:
        default = "locked"
    else:
        default = "unknown"
    return {
        "section": section_id,
        "file_default": default,
        "path": _SOUL_FILE_MAP.get(section_id, ""),
    }


def _is_soul_section_enabled(character_name: str, section_id: str) -> bool:
    """Prueft ob die Soul-Section per Template-Feature aktiviert ist.

    'personality' / 'tasks' / 'presence' sind ungated → immer.
    Andere ueber Template-Feature. beliefs/lessons/goals sind zusaetzlich
    an den Retrospect-Master-Switch gekoppelt — wenn der Char per UI
    Retrospect deaktiviert hat, fallen die drei Sections aus dem Soul-Tab
    raus, unabhaengig davon was das Template fuer beliefs/lessons/goals
    sagt.
    """
    if section_id in ("personality", "tasks", "presence"):
        return True
    feature_map = {
        "roleplay_rules": "roleplay_rules_enabled",
        "beliefs":        "beliefs_enabled",
        "lessons":        "lessons_enabled",
        "goals":          "goals_enabled",
        "soul":           "soul_enabled",
    }
    feature = feature_map.get(section_id)
    if not feature:
        return False
    try:
        from app.models.character_template import is_feature_enabled
        # Retrospect-Master-Switch: schaltet die drei Output-Sections
        # gemeinsam ab.
        if section_id in ("beliefs", "lessons", "goals"):
            if not is_feature_enabled(character_name, "retrospect_enabled"):
                return False
        return is_feature_enabled(character_name, feature)
    except Exception:
        return True


@router.get("/{character_name}/soul/files")
def get_soul_files(character_name: str) -> Dict[str, Any]:
    """Listet die fuer diesen Character verfuegbaren Soul-MD-Dateien auf.

    Berucksichtigt Template-Feature-Gates. Gibt pro Datei: section-id,
    file_default lock-status, ob die Datei existiert.
    """
    from app.models.character import get_character_dir
    char_dir = get_character_dir(character_name)
    files = []
    for section_id in ("personality", "tasks", "presence", "roleplay_rules",
                        "beliefs", "lessons", "goals", "soul"):
        if not _is_soul_section_enabled(character_name, section_id):
            continue
        meta = _soul_file_meta(section_id)
        meta["exists"] = (char_dir / meta["path"]).exists()
        files.append(meta)
    return {"character": character_name, "files": files}


@router.get("/{character_name}/soul/file/{section_id}")
def get_soul_file(character_name: str, section_id: str) -> Dict[str, Any]:
    """Liefert Inhalt + parsed Sections einer Soul-MD-Datei."""
    if section_id not in _SOUL_FILE_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown section: {section_id}")
    if not _is_soul_section_enabled(character_name, section_id):
        raise HTTPException(status_code=403, detail=f"Section '{section_id}' nicht im Template aktiv")

    from app.models.character import get_character_dir
    char_dir = get_character_dir(character_name)
    md_path = char_dir / _SOUL_FILE_MAP[section_id]

    raw = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    sections = _parse_soul_sections(raw)
    meta = _soul_file_meta(section_id)
    return {
        "character": character_name,
        "section": section_id,
        "path": meta["path"],
        "file_default": meta["file_default"],
        "raw": raw,
        "sections": sections,
        "editable_marker_token": _SOUL_EDITABLE_MARKER,
    }


@router.post("/{character_name}/soul/file/{section_id}")
async def save_soul_file(character_name: str, section_id: str, request: Request) -> Dict[str, Any]:
    """Schreibt komplette Soul-MD-Datei.

    Body: {"user_id": "...", "content": "...full MD text..."}
    """
    if section_id not in _SOUL_FILE_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown section: {section_id}")
    data = await request.json()
    user_id = data.get("user_id", "")
    content = data.get("content", "")
    if not _is_soul_section_enabled(character_name, section_id):
        raise HTTPException(status_code=403, detail=f"Section '{section_id}' nicht im Template aktiv")

    from app.models.character import get_character_dir
    char_dir = get_character_dir(character_name)
    md_path = char_dir / _SOUL_FILE_MAP[section_id]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline garantieren, ohne ueberflüssige zu sammeln
    md_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return {"status": "success", "section": section_id, "size": len(content)}


@router.put("/{character_name}/skills/{skill_name}/enabled")
async def toggle_character_skill(character_name: str, skill_name: str, request: Request) -> Dict[str, Any]:
    """Toggles a skill enabled/disabled for a character."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        enabled = data.get("enabled", True)

        config = get_character_skill_config(character_name, skill_name)
        config["enabled"] = bool(enabled)
        save_character_skill_config(character_name, skill_name, config)
        return {"status": "success", "skill": skill_name, "enabled": bool(enabled)}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Per-Character Skill Config ---

@router.get("/{character_name}/skills/{skill_name}")
def get_character_skill_config_route(character_name: str, skill_name: str) -> Dict[str, Any]:
    """Gibt die character-spezifische Skill-Konfiguration zurueck"""
    try:
        config = get_character_skill_config(character_name, skill_name)
        return {
            "character": character_name,
            "skill": skill_name,
            "config": config
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/skills/{skill_name}")
async def update_character_skill_config_route(character_name: str, skill_name: str, request: Request) -> Dict[str, Any]:
    """Speichert die character-spezifische Skill-Konfiguration (Merge)."""
    try:
        data = await request.json()
        user_id = data.get("user_id", "")
        config = data.get("config", {})
        if not isinstance(config, dict):
            raise HTTPException(status_code=400, detail="config muss ein Objekt sein")

        # Merge: Bestehende Config laden und nur uebergebene Felder updaten
        existing = get_character_skill_config(character_name, skill_name)
        if existing:
            existing.update(config)
            config = existing

        save_character_skill_config(character_name, skill_name, config)
        return {
            "status": "success",
            "character": character_name,
            "skill": skill_name,
            "config": config
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- FaceSwap / Enhancement ---

@router.post("/{character_name}/faceswap")
async def manual_face_swap(character_name: str, request: Request) -> Dict[str, Any]:
    """Manueller FaceSwap"""
    from app.skills.face_client import apply_face_swap_files
    import asyncio

    body = await request.json()
    user_id = body.get("user_id", "")
    target_image = body.get("target_image", "")
    source_image = body.get("source_image", "")
    if not target_image:
        raise HTTPException(status_code=400, detail="target_image fehlt")

    for fname in [target_image, source_image]:
        if fname and (".." in fname or "/" in fname):
            raise HTTPException(status_code=400, detail=f"Ungueltiger Dateiname: {fname}")

    images_dir = get_character_images_dir(character_name)
    target_path = images_dir / target_image

    if not target_path.exists():
        raise HTTPException(status_code=404, detail=f"Target-Bild nicht gefunden: {target_image}")

    if source_image:
        source_path = images_dir / source_image
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"Source-Bild nicht gefunden: {source_image}")
    else:
        profile_img = get_character_profile_image(character_name)
        if not profile_img:
            raise HTTPException(
                status_code=400,
                detail="Kein source_image angegeben und kein Profilbild gesetzt"
            )
        source_path = images_dir / profile_img
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"Profilbild nicht gefunden: {profile_img}")

    logger.info("Manueller FaceSwap: target=%s, source=%s, character=%s", target_image, source_path.name, character_name)

    result = await asyncio.to_thread(apply_face_swap_files, str(target_path), str(source_path))
    if result is None:
        raise HTTPException(
            status_code=422,
            detail="FaceSwap fehlgeschlagen - kein Gesicht erkannt oder Model nicht geladen"
        )

    return {
        "status": "success",
        "target_image": target_image,
        "source_image": source_path.name,
        "message": f"FaceSwap erfolgreich auf {target_image} angewendet"
    }


@router.post("/faceswap/reset")
def reset_faceswap() -> Dict[str, str]:
    """Setzt FaceSwap-Models zurueck."""
    from app.skills.face_client import reset
    reset()
    return {"status": "success", "message": "FaceSwap Models werden beim naechsten Aufruf neu geladen"}


@router.post("/{character_name}/enhance")
async def manual_face_enhance(character_name: str, request: Request) -> Dict[str, Any]:
    """Manuelles Face Enhancement"""
    from app.skills.face_client import apply_face_enhance_files
    import asyncio

    body = await request.json()
    user_id = body.get("user_id", "")
    image_name = body.get("image", "")
    if not image_name:
        raise HTTPException(status_code=400, detail="image fehlt")

    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail=f"Ungueltiger Dateiname: {image_name}")

    images_dir = get_character_images_dir(character_name)
    image_path = images_dir / image_name

    if not image_path.exists():
        raise HTTPException(status_code=404, detail=f"Bild nicht gefunden: {image_name}")

    logger.info("Manuelles Enhancement: image=%s, character=%s", image_name, character_name)

    result = await asyncio.to_thread(apply_face_enhance_files, str(image_path))
    if result is None:
        raise HTTPException(
            status_code=422,
            detail="Face Enhancement fehlgeschlagen - kein Gesicht erkannt oder Model nicht geladen"
        )

    return {
        "status": "success",
        "image": image_name,
        "message": f"Face Enhancement erfolgreich auf {image_name} angewendet"
    }


@router.post("/enhance/reset")
def reset_face_enhance() -> Dict[str, str]:
    """Setzt Face Enhancement Model zurueck."""
    from app.skills.face_client import reset
    reset()
    return {"status": "success", "message": "Face Enhancement Model wird beim naechsten Aufruf neu geladen"}


@router.post("/{character_name}/images/{image_name}/detect-characters")
async def detect_image_characters(character_name: str, image_name: str, request: Request) -> Dict[str, Any]:
    """Erkennt im Bild verwendete Characters aus reference_images Metadaten."""
    from app.models.account import get_user_profile, get_active_character
    from app.models.character import get_single_image_meta

    body = await request.json()
    user_id = body.get("user_id", "")

    all_chars = list_available_characters()
    user_profile = get_user_profile()
    # user_name = Login-Name (z.B. "admin") — ist KEINE Person die in einem
    # Bild auftauchen kann. Avatar ist der vom User gespielte Character.
    avatar_name = (get_active_character() or "").strip()
    user_name = avatar_name  # Fuer Detection (Filename-Match etc.) den Avatar nutzen

    # 1. Primaer: explizit gespeicherte character_names aus vorheriger Auswahl
    meta = get_single_image_meta(character_name, image_name)
    saved_names = (meta or {}).get("character_names")
    detected_names = []
    if saved_names and isinstance(saved_names, list):
        detected_names = saved_names
    else:
        # 2. Fallback: canonical.persons aus dem Generate-Zeitpunkt — das ist
        #    die zuverlaessigste Quelle, weil sie genau die Personen enthaelt
        #    die der Prompt-Builder ins Bild gepackt hat. Nur Nicht-Agent
        #    persons (= im Bild sichtbar, nicht der Photographer-Avatar).
        canonical = (meta or {}).get("canonical") or {}
        canon_persons = canonical.get("persons") or []
        if isinstance(canon_persons, list):
            for p in canon_persons:
                if not isinstance(p, dict):
                    continue
                _n = (p.get("name") or "").strip()
                if _n and _n not in detected_names:
                    detected_names.append(_n)

        # 3. Fallback: aus reference_images ableiten — aber NUR Person-Slots,
        #    keine Background-/Location-Refs (sonst wird der Avatar faelschlich
        #    detected weil das BG-Bild zufaellig im User-Image-Dir liegt).
        if not detected_names:
            ref_images = meta.get("reference_images", {}) if meta else {}
            _BG_SLOT_HINTS = ("background", "location", "scene", "_4", "room")
            for _slot, ref_filename in ref_images.items():
                _slot_lower = (_slot or "").lower()
                if any(h in _slot_lower for h in _BG_SLOT_HINTS):
                    continue  # Background/Location-Slot, keine Person
                matched = False
                # a) Dateiname beginnt mit Character-Name
                for c in all_chars:
                    if ref_filename.startswith(c + "_"):
                        if c not in detected_names:
                            detected_names.append(c)
                        matched = True
                        break
                if matched:
                    continue
                # b) Datei in Character-Verzeichnissen suchen (Images, Outfits, Variants)
                for c in all_chars:
                    _imgs_dir = get_character_images_dir(c)
                    _outfits_dir = get_character_outfits_dir(c)
                    _variants_dir = _outfits_dir / "variants"
                    if ((_imgs_dir / ref_filename).exists()
                            or (_outfits_dir / ref_filename).exists()
                            or (_variants_dir / ref_filename).exists()):
                        if c not in detected_names:
                            detected_names.append(c)
                        matched = True
                        break
                # c) User-Profilbild — nur wenn Slotname NICHT als BG identifiziert wurde
                #    (oben schon abgefangen)
                if not matched and user_name and user_name not in detected_names:
                    from app.models.account import get_user_images_dir
                    _user_imgs = get_user_images_dir()
                    if (_user_imgs / ref_filename).exists():
                        detected_names.append(user_name)

        # 4. Letzter Fallback: Prompt-basierte Erkennung
        if not detected_names:
            prompts = get_character_image_prompts(character_name)
            prompt = prompts.get(image_name, "")
            if prompt:
                from app.core.prompt_builder import PromptBuilder
                _pb = PromptBuilder(character_name)
                _persons = _pb.detect_persons(prompt)
                appearances = [{"name": p.name, "appearance": p.appearance} for p in _persons]
                detected_names = [p["name"] for p in appearances]

    # Underscore-Prefix-Filter (Sicherheitsnetz fuer System-Characters wie
    # _messaging_frame, falls list_available_characters cached o.ae.)
    all_chars = [c for c in all_chars if not c.startswith("_")]

    # "Agent" = der Charakter der das Bild ERSTELLT hat. Bei Bildern die ein
    # NPC dem Avatar geschickt hat (gallery_character != ersteller), steht
    # der Ersteller in meta.from_character. Sonst ist es der gallery_owner.
    agent_name = (meta or {}).get("from_character") or character_name
    available = []
    if agent_name and agent_name in all_chars:
        available.append({"name": agent_name, "type": "agent"})
    if user_name and user_name != agent_name:
        available.append({"name": user_name, "type": "user"})
    for c in all_chars:
        if c != agent_name and c != user_name:
            available.append({"name": c, "type": "character"})

    # Rooms der aktuellen Location (fuer Room-Auswahl im Dialog)
    rooms = []
    current_room_id = ""
    location_id = (meta or {}).get("location", "")
    if not location_id:
        location_id = get_character_current_location(character_name) or ""
    if location_id:
        from app.models.world import get_location
        loc_data = get_location(location_id)
        if loc_data:
            for room in loc_data.get("rooms", []):
                rooms.append({"id": room.get("id", ""), "name": room.get("name", "")})
        current_room_id = (meta or {}).get("room_id", "")
        # Room aus Slot-4 Hintergrundbild ableiten wenn nicht explizit gespeichert
        if not current_room_id:
            ref_images = meta.get("reference_images", {}) if meta else {}
            slot4_filename = ref_images.get("input_reference_image_4", "")
            if slot4_filename and loc_data:
                from app.models.world import get_gallery_image_rooms
                _img_rooms = get_gallery_image_rooms(location_id)
                _matched_room = _img_rooms.get(slot4_filename, "")
                if _matched_room:
                    current_room_id = _matched_room
        # Kein Fallback auf aktuellen Raum — wenn nicht bekannt, leer lassen

    return {
        "detected": detected_names,
        "available": available,
        "rooms": rooms,
        "current_room_id": current_room_id,
        "location_id": location_id,
    }


@router.post("/{character_name}/images/{image_name}/regenerate")
async def regenerate_character_image(character_name: str, image_name: str, request: Request) -> Dict[str, Any]:
    """Regeneriert ein Bild mit der ImageGenerationSkill-Pipeline.

    Nutzt den gespeicherten Prompt, optional verbessert durch User-Feedback,
    und generiert ein neues Bild das das alte ersetzt.
    """
    from app.skills.image_regenerate import regenerate_image
    from app.models.character import add_character_image_prompt

    body = await request.json()
    user_id = body.get("user_id", "")
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail=f"Ungueltiger Dateiname: {image_name}")

    images_dir = get_character_images_dir(character_name)
    image_path = images_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail=f"Bild nicht gefunden: {image_name}")

    prompts = get_character_image_prompts(character_name)
    prompt = prompts.get(image_name, "")
    if not prompt:
        raise HTTPException(status_code=422, detail="Kein Prompt fuer dieses Bild gespeichert.")

    # Custom-Prompt aus Dialog uebernimmt gespeicherten Prompt
    custom_prompt = body.get("custom_prompt", "").strip()
    if custom_prompt:
        prompt = custom_prompt

    improvement_request = body.get("improvement_request", "").strip()
    workflow_name = body.get("workflow", "").strip()
    backend_name = body.get("backend", "").strip()
    loras = body.get("loras")  # Optional: [{name, strength}, ...]
    model_override = body.get("model_override", "").strip()
    character_names = body.get("character_names")  # Optional: explizite Character-Auswahl
    room_id = body.get("room_id", "").strip()  # Optional: Raum-Override
    negative_prompt_override = body.get("negative_prompt", "").strip()  # Optional: Negativ-Prompt aus Dialog
    create_new = body.get("create_new", False)
    # Originale Location aus Bild-Metadaten (nicht aktuelle Character-Position)
    from app.models.character import get_single_image_meta
    _img_meta = get_single_image_meta(character_name, image_name) or {}
    original_location_id = _img_meta.get("location", "")
    agent_config = get_character_config(character_name)

    from app.core.task_queue import get_task_queue
    from app.core.task_router import resolve_queue
    _tq = get_task_queue()
    _queue = resolve_queue("image_regenerate", {}, agent_name=character_name)
    _track_id = _tq.track_start(
        "image_regenerate", "Bild regenerieren", agent_name=character_name,
        provider=backend_name or workflow_name or "",
        queue_name=_queue,
        start_running=False)

    def _run_regen():
        try:
            _success, final_prompt, actual_path = regenerate_image(character_name, str(image_path),
                prompt, improvement_request, workflow_name, backend_name, agent_config,
                loras=loras,
                model_override=model_override,
                character_names=character_names,
                room_id=room_id,
                location_id=original_location_id,
                negative_prompt_override=negative_prompt_override,
                track_id=_track_id,
                create_new=bool(create_new))
            _actual_filename = Path(actual_path).name
            if final_prompt != prompt:
                add_character_image_prompt(character_name, _actual_filename, final_prompt)
            _tq.track_finish(_track_id)
        except Exception as e:
            logger.error("Bild-Regenerierung fehlgeschlagen: %s", e)
            _tq.track_finish(_track_id, error=str(e))

    import threading
    threading.Thread(target=_run_regen, daemon=True).start()
    return {"status": "started", "image": image_name, "track_id": _track_id}


@router.post("/{character_name}/enhance-image-prompt")
async def enhance_image_prompt(character_name: str, request: Request) -> Dict[str, Any]:
    """Verbessert einen Image-Prompt via LLM direkt im Dialog.

    Body: { user_id, prompt, improvement_request, llm_override? }
    Returns: { prompt: "verbesserter prompt" }
    """
    body = await request.json()
    user_id = body.get("user_id", "")

    prompt = body.get("prompt", "").strip()
    improvement_request = body.get("improvement_request", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt fehlt")
    if not improvement_request:
        raise HTTPException(status_code=400, detail="improvement_request fehlt")

    agent_config = get_character_config(character_name)
    from app.skills.image_regenerate import enhance_prompt
    # enhance_prompt macht einen blocking LLM-Call → Threadpool, sonst
    # blockiert der Event-Loop bis das Tool-LLM antwortet (~1s+).
    import asyncio as _asyncio
    enhanced = await _asyncio.to_thread(
        enhance_prompt, prompt, improvement_request, agent_config)
    return {"prompt": enhanced}


@router.post("/{character_name}/rebuild-image-prompt")
async def rebuild_image_prompt(character_name: str, request: Request) -> Dict[str, Any]:
    """Baut den Image-Prompt neu auf basierend auf dem Adapter des gewaehlten Workflows.

    Quelle der Source-Werte (mood, outfit, expression, location, ...):
      1. PRIMAER: gespeichertes `canonical`-Dict aus dem image.json (vom Erstellungszeitpunkt)
      2. FALLBACK: aktueller Character-State (nur fuer alte Bilder ohne canonical)

    Body: { user_id, workflow?, canonical?, scene_text? }
    Returns: { prompt, target_model, source: "saved"|"current" }
    """
    import asyncio

    body = await request.json()
    user_id = body.get("user_id", "")
    workflow_name = body.get("workflow", "").strip()
    saved_canonical = body.get("canonical") or None
    scene_text = body.get("scene_text", "").strip()
    location_id = body.get("location_id", "").strip()
    room_id = body.get("room_id", "").strip()
    reference_images = body.get("reference_images") or {}

    def _build():
        # Closure: location_id/room_id sind im outer scope nonlocal
        nonlocal location_id, room_id

        # Fallback: location_id/room_id aus reference_image_4 (Slot 4 = Raum-Hintergrund) ableiten
        if not (location_id or room_id) and reference_images:
            ref_room_img = reference_images.get("input_reference_image_4", "")
            if ref_room_img:
                try:
                    from app.models.world import find_room_by_gallery_image
                    _loc_from_ref, _room_from_ref = find_room_by_gallery_image(ref_room_img)
                    if _loc_from_ref:
                        location_id = location_id or _loc_from_ref
                        room_id = room_id or _room_from_ref
                        logger.info("rebuild: location/room aus Reference-Image '%s' aufgeloest (loc=%s room=%s)",
                                    ref_room_img, location_id, room_id)
                except Exception as _e:
                    logger.debug("Reference-Image-Aufloesung fehlgeschlagen: %s", _e)

        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        img_skill = None
        for skill in sm.skills:
            if skill.__class__.__name__ == "ImageGenerationSkill":
                img_skill = skill
                break
        if not img_skill:
            raise HTTPException(status_code=503, detail="ImageGenerationSkill nicht verfuegbar")

        # Workflow + Target-Model bestimmen
        active_wf = None
        if workflow_name:
            active_wf = next(
                (wf for wf in getattr(img_skill, "comfy_workflows", []) if wf.name == workflow_name),
                None)
        if not active_wf:
            active_wf = getattr(img_skill, "_default_workflow", None)

        from app.core.prompt_adapters import (
            get_target_model, render as adapter_render,
            maybe_enhance_via_llm, dict_to_canonical)

        wf_image_model = getattr(active_wf, "image_model", "") if active_wf else ""
        wf_file = getattr(active_wf, "workflow_file", "") if active_wf else ""
        target_model = get_target_model(wf_image_model, wf_file)

        # 1) PRIMAERE Quelle: gespeichertes canonical
        if saved_canonical and isinstance(saved_canonical, dict):
            pv = dict_to_canonical(saved_canonical)
            # Style/Negative aus Workflow-Config bevorzugen falls Workflow gewechselt wurde,
            # sonst gespeicherte Werte beibehalten
            if active_wf and active_wf.prompt_style:
                pv.prompt_style = active_wf.prompt_style
            if active_wf and active_wf.negative_prompt:
                pv.negative_prompt = active_wf.negative_prompt

            # sanitize_scene_prompt auch beim Rebuild ausfuehren — damit der
            # neue Outfit-Extraction-Filter (wearing/posing/dressed) auch fuer
            # Re-Creations greift. Sanitize ist idempotent: bei bereits
            # bereinigten scenes findet es nichts und bleibt no-op.
            if pv.scene_prompt:
                from app.core.prompt_builder import PromptBuilder as _PB
                _rebuild_builder = _PB(character_name)
                pv.scene_prompt = _rebuild_builder.sanitize_scene_prompt(pv.scene_prompt, pv)

            # Outfit-Enrichment: Wenn canonical.outfits leer aber reference_image_1
            # vorhanden, das Outfit ueber die Bild-Datei aufloesen
            if not pv.prompt_outfits and reference_images:
                ref1 = reference_images.get("input_reference_image_1", "")
                if ref1:
                    try:
                        from app.models.character import find_outfit_by_image
                        _outfit = find_outfit_by_image(character_name, ref1)
                        if _outfit:
                            _outfit_text = (_outfit.get("outfit") or "").strip()
                            if _outfit_text:
                                _label = pv.persons[0].actor_label if pv.persons else character_name
                                pv.prompt_outfits[1] = f"{_label} is wearing {_outfit_text}"
                                logger.info("rebuild: Outfit '%s' aus Reference-Image '%s' aufgeloest",
                                            _outfit.get("name", "?"), ref1)
                    except Exception as _e:
                        logger.debug("Outfit-Enrichment fehlgeschlagen: %s", _e)
            # Location-Enrichment: wenn canonical.location zu kurz (nur Name, keine Description),
            # aus world.json den Raum-Description nachladen
            if pv.prompt_location and len(pv.prompt_location) < 30 and (location_id or room_id):
                try:
                    from app.models.world import get_location, get_room_by_id
                    from datetime import datetime as _dt
                    _loc_data = get_location(location_id) if location_id else None
                    if _loc_data:
                        _hour = _dt.now().hour
                        _is_day = 6 <= _hour < 18
                        _desc = ""
                        # Raum bevorzugt
                        if room_id:
                            _room = get_room_by_id(_loc_data, room_id)
                            if _room:
                                _desc = (_room.get("image_prompt_day", "") if _is_day else _room.get("image_prompt_night", "")) \
                                        or _room.get("description", "")
                        if not _desc:
                            _desc = (_loc_data.get("image_prompt_day", "") if _is_day else _loc_data.get("image_prompt_night", "")) \
                                    or _loc_data.get("description", "")
                        if _desc:
                            pv.prompt_location = f"{pv.prompt_location}, {_desc}"
                            logger.info("rebuild: Location enriched fuer kurze canonical.location (room=%s loc=%s)",
                                        room_id, location_id)
                except Exception as _e:
                    logger.debug("Location-Enrichment fehlgeschlagen: %s", _e)
            source = "saved"
        else:
            # 2) FALLBACK: aktueller State (nur fuer alte Bilder ohne canonical)
            from app.core.prompt_builder import PromptBuilder, EntryPointConfig
            builder = PromptBuilder(character_name)
            persons = builder.detect_persons(scene_text or "")
            pv = builder.collect_context(
                persons, EntryPointConfig.chat(),
                prompt_text=scene_text or "",
                photographer_mode=False,
                set_profile=False)
            if scene_text:
                pv.scene_prompt = builder.sanitize_scene_prompt(scene_text, pv)
            pv.prompt_style = (active_wf.prompt_style if active_wf and active_wf.prompt_style else "") or "photorealistic"
            pv.negative_prompt = active_wf.negative_prompt if active_wf and active_wf.negative_prompt else ""
            source = "current"

            # Outfit-Enrichment auch im current-state Pfad: bei Bildern ohne canonical
            # versuchen das ORIGINAL-Outfit ueber reference_image_1 wiederherzustellen
            # (statt das aktuelle Char-Outfit zu nutzen).
            if reference_images:
                ref1 = reference_images.get("input_reference_image_1", "")
                if ref1:
                    try:
                        from app.models.character import find_outfit_by_image
                        _outfit = find_outfit_by_image(character_name, ref1)
                        if _outfit:
                            _outfit_text = (_outfit.get("outfit") or "").strip()
                            if _outfit_text and persons:
                                _label = persons[0].actor_label or character_name
                                pv.prompt_outfits[1] = f"{_label} is wearing {_outfit_text}"
                                logger.info("rebuild (current): Outfit '%s' aus Reference-Image '%s' aufgeloest",
                                            _outfit.get("name", "?"), ref1)
                                source = "current+ref_outfit"
                    except Exception as _e:
                        logger.debug("Outfit-Enrichment fehlgeschlagen: %s", _e)

        assembled = adapter_render(pv, target_model)
        template_prompt = assembled["input_prompt_positiv"]

        # Optional LLM-Enhancement (zentral via Workflow-Config)
        wf_instruction = getattr(active_wf, "prompt_instruction", "") if active_wf else ""
        final_prompt, _method = maybe_enhance_via_llm(
            template_prompt, pv,
            target_model=target_model,
            prompt_instruction=wf_instruction)
        return {"prompt": final_prompt, "target_model": target_model, "source": source}

    return await asyncio.to_thread(_build)


@router.post("/{character_name}/images/{image_name}/suggest-animate-prompt")
async def suggest_animate_prompt(character_name: str, image_name: str, request: Request) -> Dict[str, str]:
    """Generiert einen Animation-Prompt basierend auf der Bildanalyse via Tools-LLM."""
    import asyncio

    body = await request.json()
    user_id = body.get("user_id", "")
    custom_system_prompt = body.get("system_prompt", "")
    llm_override = body.get("llm_override", "").strip()

    images_dir = get_character_images_dir(character_name)
    image_path = images_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    def _generate_prompt() -> str:
        from app.models.character import _load_single_image_meta
        meta = _load_single_image_meta(character_name, image_name)

        # Bildanalyse aus Metadaten lesen oder neu generieren
        image_analysis = meta.get("image_analysis", "")
        if not image_analysis:
            logger.info("[suggest-animate] Keine Bildanalyse vorhanden, generiere neu...")
            try:
                from app.skills.image_generation_skill import ImageGenerationSkill
                skill = ImageGenerationSkill({})
                image_analysis = skill._generate_image_analysis(str(image_path), character_name)
                if image_analysis:
                    logger.info("[suggest-animate] Bildanalyse generiert (%d Zeichen)", len(image_analysis))
                    add_character_image_metadata(character_name, image_name, {
                        "image_analysis": image_analysis,
                    })
            except Exception as e:
                logger.warning("[suggest-animate] Bildanalyse fehlgeschlagen: %s", e)

        if not image_analysis:
            raise ValueError("Bildanalyse nicht verfuegbar")

        logger.info("[suggest-animate] Bildanalyse vorhanden (%d Zeichen), rufe LLM auf... (llm_override=%s)", len(image_analysis), llm_override or "")

        from app.core.llm_router import llm_call
        from app.core.prompt_templates import render_task
        default_system, user_prompt = render_task(
            "animation_prompt", image_analysis=image_analysis)
        system_content = custom_system_prompt or default_system
        response = llm_call(
            task="instagram_caption",
            system_prompt=system_content,
            user_prompt=user_prompt,
            agent_name=character_name)
        result = (response.content or "").strip().strip('"').strip("'")
        logger.info("[suggest-animate] Prompt generiert: %s", result[:100])
        return result

    try:
        prompt = await asyncio.get_event_loop().run_in_executor(None, _generate_prompt)
        return {"prompt": prompt}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("[suggest-animate] Fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{character_name}/images/{image_name}/animate")
async def animate_character_image(character_name: str, image_name: str, request: Request) -> Dict[str, Any]:
    """Animiert ein Galerie-Bild als Video via img2video ComfyUI Workflow."""
    body = await request.json()
    user_id = body.get("user_id", "")
    if ".." in image_name or "/" in image_name:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    images_dir = get_character_images_dir(character_name)
    image_path = images_dir / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    prompt = body.get("prompt", "").strip()
    if not prompt:
        prompts = get_character_image_prompts(character_name)
        prompt = prompts.get(image_name, "")
    if not prompt:
        raise HTTPException(status_code=422, detail="Kein Prompt angegeben")

    service = body.get("service", "").strip()
    loras_high = body.get("loras_high")  # [{name, strength}, ...] oder None
    loras_low = body.get("loras_low")    # [{name, strength}, ...] oder None

    from app.core.task_queue import get_task_queue
    _tq = get_task_queue()
    _track_id = _tq.track_start(
        "image_animate", "Bild animieren", agent_name=character_name,
        start_running=False)

    def _run_animate():
        _tq.track_activate(_track_id)
        try:
            from app.skills.animate import animate_image
            from datetime import datetime
            from app.core.llm_queue import get_llm_queue, Priority as _P

            stem = Path(image_name).stem
            video_name = f"{stem}.mp4"
            output_path = str(images_dir / video_name)

            # Ueber Provider-Queue ausfuehren (GPU-Serialisierung + Queue-Panel)
            success = get_llm_queue().submit_gpu_task(
                task_type="image_animate",
                priority=_P.IMAGE_GEN,
                callable_fn=lambda: animate_image(
                    str(image_path), prompt, output_path,
                    service=service, loras_high=loras_high, loras_low=loras_low),
                agent_name=character_name,
                label="Animation",
                gpu_type="comfyui")

            if not success:
                _tq.track_finish(_track_id, error="Animation fehlgeschlagen")
                return

            # Video-Info in bestehende Bild-Metadaten schreiben
            add_character_image_metadata(character_name, image_name, {
                "animate_prompt": prompt,
                "animate_created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })
            _tq.track_finish(_track_id)
        except Exception as e:
            logger.error("Animation fehlgeschlagen: %s", e)
            _tq.track_finish(_track_id, error=str(e))

    import threading
    threading.Thread(target=_run_animate, daemon=True).start()
    return {"status": "started", "image": image_name, "track_id": _track_id}


# ---------------------------------------------------------------------------
# Character Export / Import
# ---------------------------------------------------------------------------

@router.get("/{character_name}/export")
def export_character(
    character_name: str,
    include_chats: bool = Query(False, description="Include chat history"),
    include_stories: bool = Query(False, description="Include story progress"),
) -> StreamingResponse:
    """Exports a character as a ZIP bundle (DB rows + filesystem dir).

    The ZIP carries a manifest.json plus `files/` (char dir contents) and
    `db/<table>.json` slices for every table the character owns rows in
    (profile/config, state, memories, summaries, knowledge, inventory,
    outfits, schedule, secrets, relationships, image metadata, ...).
    """
    from app.core.character_io import export_character_to_zip
    from app.core.db import get_connection

    char_dir = get_character_dir(character_name)
    conn = get_connection()
    db_row = conn.execute(
        "SELECT 1 FROM characters WHERE name=?", (character_name,)
    ).fetchone()
    if not char_dir.exists() and not db_row:
        raise HTTPException(
            status_code=404,
            detail=f"Character '{character_name}' not found",
        )

    try:
        zip_bytes = export_character_to_zip(
            character_name,
            include_chats=include_chats,
            include_stories=include_stories,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Export failed for %s", character_name)
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    filename = f"{character_name}_export.zip"
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_character(
    file: UploadFile = File(...),
    overwrite: bool = Query(False, description="Replace existing character"),
) -> Dict[str, Any]:
    """Imports a character ZIP produced by the export endpoint.

    Restores both the filesystem char dir and all owned DB rows. With
    overwrite=true, existing DB rows and the char dir are wiped first.
    """
    from app.core.character_io import import_character_from_zip

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only ZIP files are allowed")

    content = await file.read()
    try:
        return import_character_from_zip(content, overwrite=overwrite)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Import failed")
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")


# --- ComfyUI Workflow Endpoints ---

@router.get("/{character_name}/skills/image_generation/workflows")
def get_imagegen_workflows(character_name: str) -> Dict[str, Any]:
    """Gibt alle verfuegbaren Generierungs-Optionen zurueck:
    ComfyUI-Workflows + andere Backends (A1111, Mammouth, CivitAI, etc.)."""
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        raise HTTPException(status_code=404, detail="ImageGeneration skill not found")

    # Aktuell unavailable Backends frisch pruefen — sonst zeigt der Dialog
    # einen Backend dauerhaft als "nicht verfuegbar" an, obwohl der Service
    # zwischenzeitlich wieder online gegangen ist (z.B. ComfyUI-3090 nach
    # Restart). Recovery-Hook in check_availability triggert auch
    # channel_health.force_poll(), sodass nachgelagerte GPU-Routing-Entscheidungen
    # ebenfalls den frischen Status sehen.
    for _b in imagegen.backends:
        if _b.instance_enabled and not _b.available:
            try:
                _b.check_availability()
            except Exception:
                pass

    workflows = imagegen.get_comfy_workflows()
    agent_config = get_character_skill_config(character_name, "image_generation") or {}
    active = agent_config.get("comfy_workflow", workflows[0]["name"] if workflows else "")
    workflow_models = agent_config.get("workflow_models", {})
    workflow_loras = agent_config.get("workflow_loras", {})
    comfy_seed = int(agent_config.get("comfy_seed", 0))

    # Alle verfuegbaren Generierungs-Optionen sammeln
    options = []

    # ComfyUI-Workflows als Optionen.
    # Cost = guenstigstes verfuegbares Backend fuer diesen Workflow
    # (live, beruecksichtigt per-Agent enabled). Wenn keins verfuegbar,
    # wird die Option als unavailable=True markiert — UI kann das ausgrauen.
    workflow_objs = imagegen.comfy_workflows
    for wf in workflows:
        wf_name = wf["name"]
        char_model = workflow_models.get(wf_name, "")
        wf_obj = next((w for w in workflow_objs if w.name == wf_name), None)
        wf_backends = imagegen.list_available_backends(
            character_name=character_name, workflow=wf_obj) if wf_obj else []
        wf_cost = wf_backends[0].cost if wf_backends else None
        # Target-Style fuer UI-Hinweis (Workflow.image_model > Dateiname > Default)
        try:
            from app.core.prompt_adapters import get_target_model as _gtm
            _target_style = _gtm(
                getattr(wf_obj, "image_model", "") if wf_obj else "",
                wf.get("workflow_file", "") if isinstance(wf, dict) else getattr(wf_obj, "workflow_file", ""),
                "")
        except Exception:
            _target_style = "z_image"
        options.append({
            "type": "workflow",
            "name": wf_name,
            "label": f"ComfyUI: {wf_name}",
            "has_loras": wf.get("has_loras", False),
            "has_seed": wf.get("has_seed", False),
            "default_loras": wf.get("default_loras", []),
            "model_type": wf.get("model_type", ""),
            "default_model": char_model or wf.get("default_model", ""),
            "env_model": wf.get("default_model", ""),
            "char_model": char_model,
            "char_loras": workflow_loras.get(wf_name),
            "comfy_seed": comfy_seed,
            "filter": wf.get("filter", ""),
            "negative_prompt": wf.get("negative_prompt", ""),
            "cost": wf_cost,
            "available": bool(wf_backends),
            "available_backends": [b.name for b in wf_backends],
            "target_model": _target_style,
        })

    # Nicht-ComfyUI Backends als Optionen
    agent_instances = agent_config.get("instances", {})
    for b in imagegen.backends:
        if b.api_type == "comfyui":
            continue
        if not b.available:
            continue
        # Per-Agent enabled check
        agent_inst = agent_instances.get(b.name, {})
        is_enabled = bool(agent_inst["enabled"]) if "enabled" in agent_inst else b.instance_enabled
        if not is_enabled:
            continue
        # Target-Style aus Backend-Modellname ableiten (z.B. Qwen-Image -> qwen,
        # FLUX -> flux, Z-Image-URN -> z_image). UI kann das als Hinweis anzeigen.
        try:
            from app.core.prompt_adapters import get_target_model as _gtm
            _target_style = _gtm("", "", getattr(b, 'model', ""))
        except Exception:
            _target_style = "z_image"
        opt = {
            "type": "backend",
            "name": b.name,
            "label": b.name,
            "negative_prompt": getattr(b, 'negative_prompt', ""),
            "cost": b.cost,
            "available": True,
            "target_model": _target_style,
        }
        # Backend mit Modellliste (z.B. Together.ai) — direkt als Auswahl anbieten
        backend_models = getattr(b, 'available_models', [])
        if backend_models:
            opt["models"] = backend_models
            opt["default_model"] = getattr(b, 'model', backend_models[0])
        options.append(opt)

    # Nach Cost sortieren — UI kann "billigster zuerst" anzeigen
    options.sort(key=lambda o: (o.get("cost") if o.get("cost") is not None else 999999, o.get("label", "")))

    # Default-Vorauswahl pro Bereich aus .env
    defaults = {}
    for env_key, area in [
        ("OUTFIT_IMAGEGEN_DEFAULT", "outfit"),
        ("EXPRESSION_IMAGEGEN_DEFAULT", "expression"),
        ("LOCATION_IMAGEGEN_DEFAULT", "location"),
        ("SKILL_INSTAGRAM_IMAGEGEN_DEFAULT", "instagram"),
    ]:
        val = os.environ.get(env_key, "").strip()
        if val:
            defaults[area] = val  # z.B. "workflow:Z-Image" oder "backend:CivitAI"

    return {
        "character": character_name,
        "workflows": workflows,
        "active_workflow": active,
        "options": options,
        "defaults": defaults,
    }


@router.get("/{character_name}/skills/image_generation/available-loras")
def get_available_loras(character_name: str) -> Dict[str, Any]:
    """Gibt alle verfuegbaren LoRAs zurueck (aus Startup-Cache)."""
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"loras": []}

    loras = imagegen.get_cached_loras()
    return {"loras": ["None"] + loras}


@router.get("/{character_name}/skills/image_generation/available-checkpoints")
def get_available_checkpoints(character_name: str, model_type: str = "") -> Dict[str, Any]:
    """Gibt alle verfuegbaren Checkpoints/UNets zurueck, gruppiert nach Service.

    model_type: 'unet' | 'checkpoint' — filtert die gecachte Liste.
    Leer = beide Listen kombiniert.

    Returns:
        models: Flat-Liste aller Modelle (Backward-Compat)
        models_by_service: {service_name: [model1, model2, ...]}
    """
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        return {"models": [], "models_by_service": {}}

    models = imagegen.get_cached_checkpoints(model_type)
    models_by_service = imagegen.get_cached_checkpoints_by_service(model_type)
    return {"models": models, "models_by_service": models_by_service}


@router.post("/{character_name}/skills/image_generation/refresh-model-cache")
def refresh_model_cache(character_name: str) -> Dict[str, Any]:
    """Laedt den ComfyUI Model-/LoRA-Cache neu (z.B. nach Hinzufuegen neuer Modelle)."""
    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        raise HTTPException(status_code=404, detail="ImageGeneration skill not found")

    imagegen.load_comfyui_model_cache()
    by_svc = imagegen.get_cached_checkpoints_by_service()
    return {
        "status": "success",
        "checkpoints": len(imagegen.get_cached_checkpoints("checkpoint")),
        "unet_models": len(imagegen.get_cached_checkpoints("unet")),
        "loras": len(imagegen.get_cached_loras()),
        "services": list(by_svc.keys()),
    }


@router.put("/{character_name}/skills/image_generation/workflow")
def set_imagegen_workflow(character_name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Setzt den aktiven ComfyUI-Workflow fuer einen Character."""
    user_id = body.get("user_id", "")
    workflow_name = body.get("workflow", "")
    if not workflow_name:
        raise HTTPException(status_code=400, detail="user_id and workflow required")

    sm = get_skill_manager()
    imagegen = sm.get_skill("image_generation")
    if not imagegen:
        raise HTTPException(status_code=404, detail="ImageGeneration skill not found")

    # Validate workflow name
    valid_names = [wf["name"] for wf in imagegen.get_comfy_workflows()]
    if workflow_name not in valid_names:
        raise HTTPException(status_code=400, detail=f"Unknown workflow: {workflow_name}")

    agent_config = get_character_skill_config(character_name, "image_generation") or {}
    agent_config["comfy_workflow"] = workflow_name
    save_character_skill_config(character_name, "image_generation", agent_config)

    return {
        "status": "success",
        "character": character_name,
        "active_workflow": workflow_name,
    }


@router.post("/{character_name}/skills/image_generation/workflow-model")
async def set_workflow_model(character_name: str, request: Request) -> Dict[str, Any]:
    """Setzt das per-Character Modell fuer einen Workflow (ueberschreibt .env Default)."""
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    workflow_name = data.get("workflow_name", "").strip()
    model_name = data.get("model_name", "").strip()

    agent_config = get_character_skill_config(character_name, "image_generation") or {}
    workflow_models = agent_config.get("workflow_models", {})
    if model_name:
        workflow_models[workflow_name] = model_name
    else:
        workflow_models.pop(workflow_name, None)
    agent_config["workflow_models"] = workflow_models
    save_character_skill_config(character_name, "image_generation", agent_config)

    return {"status": "success", "workflow_name": workflow_name, "model_name": model_name}


@router.post("/{character_name}/skills/image_generation/workflow-loras")
async def set_workflow_loras(character_name: str, request: Request) -> Dict[str, Any]:
    """Setzt per-Character LoRA-Overrides fuer einen Workflow (ueberschreibt .env Defaults).

    Body: {user_id, workflow_name, loras: [{name, strength}, ...] oder null zum Zuruecksetzen}
    """
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    workflow_name = data.get("workflow_name", "").strip()
    loras = data.get("loras")  # Liste oder null
    if not workflow_name:
        raise HTTPException(status_code=400, detail="workflow_name fehlt")

    agent_config = get_character_skill_config(character_name, "image_generation") or {}
    workflow_loras = agent_config.get("workflow_loras", {})
    if loras is not None:
        # Normalisieren: strength als float sicherstellen
        normalized = []
        for l in loras:
            name = (l.get("name") or "").strip() or "None"
            try:
                strength = float(l.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            normalized.append({"name": name, "strength": strength})
        workflow_loras[workflow_name] = normalized
    else:
        workflow_loras.pop(workflow_name, None)
    agent_config["workflow_loras"] = workflow_loras
    save_character_skill_config(character_name, "image_generation", agent_config)

    return {"status": "success", "workflow_name": workflow_name, "loras": loras}


@router.post("/{character_name}/skills/image_generation/comfy-seed")
async def set_comfy_seed(character_name: str, request: Request) -> Dict[str, Any]:
    """Setzt den ComfyUI-Seed fuer einen Character (0 = auto-generieren beim naechsten Aufruf)."""
    data = await request.json()
    user_id = data.get("user_id", "").strip()
    seed_value = int(data.get("seed", 0))

    agent_config = get_character_skill_config(character_name, "image_generation") or {}
    agent_config["comfy_seed"] = seed_value
    save_character_skill_config(character_name, "image_generation", agent_config)

    return {"status": "success", "comfy_seed": seed_value}


# --- VideoGeneration Skill Config ---

@router.get("/{character_name}/skills/video_generation/options")
def get_videogen_options(character_name: str) -> Dict[str, Any]:
    """Gibt alle Auswahl-Optionen fuer die VideoGen-Config zurueck:
    ImageGen Backends/Workflows/Models/LoRAs + Animation Services/LoRAs."""
    sm = get_skill_manager()

    # --- ImageGen Optionen (Workflows + Backends) ---
    imagegen = sm.get_skill("image_generation")
    imagegen_options = []
    all_models = []
    imagegen_loras = ["None"]
    models_checkpoint = []
    models_unet = []
    models_checkpoint_by_service = {}
    models_unet_by_service = {}
    if imagegen:
        # Workflows
        for wf in imagegen.get_comfy_workflows():
            imagegen_options.append({
                "type": "workflow",
                "name": wf["name"],
                "label": f"ComfyUI: {wf['name']}",
                "has_loras": wf.get("has_loras", False),
                "default_loras": wf.get("default_loras", []),
                "model_type": wf.get("model_type", ""),
                "default_model": wf.get("default_model", ""),
                "filter": wf.get("filter", ""),
            })
        # Nicht-ComfyUI Backends
        for b in imagegen.backends:
            if b.api_type == "comfyui" or not b.available:
                continue
            opt: Dict[str, Any] = {
                "type": "backend",
                "name": b.name,
                "label": b.name,
            }
            backend_models = getattr(b, 'available_models', [])
            if backend_models:
                opt["models"] = backend_models
                opt["default_model"] = getattr(b, 'model', backend_models[0])
            imagegen_options.append(opt)
        # Modelle nach Typ getrennt + LoRAs
        models_checkpoint = imagegen.get_cached_checkpoints("checkpoint")
        models_unet = imagegen.get_cached_checkpoints("unet")
        models_checkpoint_by_service = imagegen.get_cached_checkpoints_by_service("checkpoint")
        models_unet_by_service = imagegen.get_cached_checkpoints_by_service("unet")
        imagegen_loras = ["None"] + imagegen.get_cached_loras()

    # --- Animation Optionen ---
    from app.skills.animate import get_animate_services
    animate_services = get_animate_services()
    # Animate LoRAs (WAN-gefiltert)
    animate_loras = ["None"]
    if imagegen:
        wan_loras = [l for l in imagegen.get_cached_loras() if l.lower().startswith("wan")]
        animate_loras = ["None"] + wan_loras

    # --- Aktuelle per-Character Config ---
    current_config = get_character_skill_config(character_name, "video_generation") or {}

    return {
        "imagegen_options": imagegen_options,
        "models_checkpoint": models_checkpoint,
        "models_unet": models_unet,
        "models_checkpoint_by_service": models_checkpoint_by_service,
        "models_unet_by_service": models_unet_by_service,
        "imagegen_loras": imagegen_loras,
        "animate_services": animate_services,
        "animate_loras": animate_loras,
        "current_config": {
            "imagegen_backend": current_config.get("imagegen_backend", ""),
            "imagegen_workflow": current_config.get("imagegen_workflow", ""),
            "imagegen_model": current_config.get("imagegen_model", ""),
            "imagegen_loras": current_config.get("imagegen_loras", []),
            "animate_service": current_config.get("animate_service", ""),
            "animate_loras_high": current_config.get("animate_loras_high", []),
            "animate_loras_low": current_config.get("animate_loras_low", []),
        },
    }


@router.post("/{character_name}/skills/video_generation/config")
async def save_videogen_config(character_name: str, request: Request) -> Dict[str, Any]:
    """Speichert die VideoGen-Config (ImageGen + Animation Einstellungen)."""
    data = await request.json()
    user_id = data.get("user_id", "").strip()

    config = get_character_skill_config(character_name, "video_generation") or {}

    # ImageGen-Felder
    for key in ("imagegen_backend", "imagegen_workflow", "imagegen_model", "animate_service"):
        if key in data:
            config[key] = str(data[key]).strip()

    # LoRA-Listen normalisieren
    def _normalize_loras(loras):
        if not loras:
            return []
        out = []
        for l in loras:
            name = (l.get("name") or "").strip() or "None"
            try:
                strength = float(l.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            out.append({"name": name, "strength": strength})
        return out

    for key in ("imagegen_loras", "animate_loras_high", "animate_loras_low"):
        if key in data:
            config[key] = _normalize_loras(data[key])

    save_character_skill_config(character_name, "video_generation", config)
    return {"status": "success"}


# --- Memory/Knowledge Endpoints ---

@router.delete("/{character_name}/knowledge/{entry_id}")
def delete_single_knowledge(character_name: str, entry_id: str) -> Dict[str, Any]:
    """Loescht einen einzelnen Memory-Eintrag."""
    from app.models.memory import delete_memory
    if delete_memory(character_name, entry_id):
        return {"status": "success", "deleted": entry_id}
    raise HTTPException(status_code=404, detail="Entry not found")


@router.delete("/{character_name}/knowledge")
def clear_character_knowledge(character_name: str) -> Dict[str, Any]:
    """Loescht alle Memory-Eintraege eines Characters."""
    from app.models.memory import clear_memories
    m_count = clear_memories(character_name)
    return {"status": "success", "deleted_count": m_count}


# ---------------------------------------------------------------------------
# Memory-Modal v2 — Tab-spezifische Endpoints
# Plan: development_instructions/plan-memory-window-redesign.md
# ---------------------------------------------------------------------------

def _score_memory_no_mutate(entry: Dict[str, Any], current_message: str = "") -> float:
    """retrieve_relevant_memories ohne Side-Effects (kein access_count-Bump).

    Spiegelt die Score-Formel aus app/models/memory.py:retrieve_relevant_memories
    fuer die Read-Only-Anzeige im "Heute"-Tab.
    """
    from datetime import datetime as _dt
    from app.models.memory import _compute_decay, _keyword_overlap, _recency_boost

    decay = _compute_decay(entry)
    importance = entry.get("importance", 3)
    search_text = entry.get("content", "") + " " + " ".join(entry.get("tags", []))
    relevance = _keyword_overlap(search_text, current_message) if current_message else 0.0
    type_bonus = 0.0
    mtype = entry.get("memory_type")
    if mtype == "commitment":
        if "completed" not in entry.get("tags", []):
            type_bonus = 0.3
    elif mtype == "episodic":
        type_bonus = 0.1
    try:
        ts = _dt.fromisoformat(entry.get("timestamp", ""))
        age_days = max(0, (_dt.now() - ts).total_seconds() / 86400)
    except (ValueError, TypeError):
        age_days = 30.0
    recency = _recency_boost(age_days)
    return importance * decay * recency * (1.0 + relevance * 2.0 + type_bonus)


def _bucket_state_lane(events: List[Dict[str, Any]],
                       max_unbucketed: int = 50) -> Dict[str, Any]:
    """Hybrid-Verdichtung: ueber `max_unbucketed` Events pro Stunde gruppieren.

    Erwartet Events sortiert (oldest first). Liefert
    {bucketed: bool, points: [{ts, value, count?}], buckets?: [{hour, dominant, items}]}.
    """
    if len(events) <= max_unbucketed:
        return {"bucketed": False, "points": events}
    # Stunden-Bucketing: pro Stunden-Slot die dominante value (haeufigste)
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        ts = ev.get("ts") or ev.get("timestamp") or ""
        hour = ts[:13]  # 'YYYY-MM-DDTHH'
        buckets.setdefault(hour, []).append(ev)
    out_points = []
    out_buckets = []
    for hour in sorted(buckets.keys()):
        items = buckets[hour]
        # dominant = most frequent value
        from collections import Counter
        cnt = Counter(it.get("value", "") for it in items)
        dominant, _ = cnt.most_common(1)[0]
        # Repraesentanten-TS = letzter Event in dieser Stunde
        out_points.append({
            "ts": items[-1].get("ts") or items[-1].get("timestamp"),
            "value": dominant,
            "count": len(items),
        })
        out_buckets.append({
            "hour": hour,
            "dominant": dominant,
            "items": items,
        })
    return {"bucketed": True, "points": out_points, "buckets": out_buckets}


@router.get("/{character_name}/memory/today")
def memory_today(character_name: str) -> Dict[str, Any]:
    """Tab "Heute": Status, 24h-Lanes, Top-K aktuell relevante Memories.

    Read-only — kein access_count-Bump beim Anzeigen.
    """
    from datetime import datetime, timedelta
    from app.core.db import get_connection
    from app.models.memory import load_memories, load_mood_history
    from app.models.character import get_character_profile, get_known_locations
    from app.models.world import (get_location_name, resolve_location,
                                   get_room_by_id, _load_world_data)

    profile = get_character_profile(character_name)
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()

    # --- 24h State-History (alle Types) — fuer Lanes ---
    conn = get_connection()
    rows = conn.execute(
        "SELECT ts, state_json FROM state_history "
        "WHERE character_name=? AND ts>=? ORDER BY ts ASC",
        (character_name, cutoff),
    ).fetchall()
    activity_lane: List[Dict[str, Any]] = []
    location_lane: List[Dict[str, Any]] = []
    effects_lane: List[Dict[str, Any]] = []
    last_warning: Optional[Dict[str, Any]] = None
    import json as _json
    for ts, state_json in rows:
        try:
            s = _json.loads(state_json or "{}")
        except Exception:
            continue
        t = s.get("type", "")
        v = s.get("value", "")
        ev = {"ts": s.get("timestamp", ts), "value": v}
        if t == "activity":
            activity_lane.append(ev)
        elif t == "location":
            # Location-IDs in Lesbare Namen aufloesen — fuer UI-Anzeige.
            ev["value"] = get_location_name(v) or v
            location_lane.append(ev)
        elif t == "effects":
            effects_lane.append(ev)
        elif t in ("access_denied", "forced_action"):
            last_warning = {"type": t, "value": v, "ts": ev["ts"]}

    # --- 24h Mood-History ---
    full_mood = load_mood_history(character_name)
    mood_lane = [m for m in full_mood if m.get("timestamp", "") >= cutoff]

    # --- "Seit"-Zeitstempel des aktuellen Zustands ---
    def _last_change_ts(lane: List[Dict[str, Any]], current: str) -> Optional[str]:
        # Letzter Wechsel ZUR aktuellen Value (von oldest nach newest scannen).
        last_ts = None
        prev = None
        for ev in lane:
            if ev.get("value") != prev and ev.get("value") == current:
                last_ts = ev.get("ts")
            prev = ev.get("value")
        return last_ts

    current_activity = profile.get("current_activity") or ""
    current_location_id = profile.get("current_location") or ""
    current_room_id = profile.get("current_room") or ""
    current_mood = profile.get("current_feeling") or profile.get("current_mood") or ""

    # Namen aufloesen (location/room sind oft UUIDs in der DB)
    location_name = get_location_name(current_location_id) if current_location_id else ""
    room_name = ""
    if current_location_id and current_room_id:
        loc = resolve_location(current_location_id)
        if loc:
            room = get_room_by_id(loc, current_room_id)
            if room:
                room_name = room.get("name", "")

    # "Seit"-Fallback: wenn 24h-Lane leer, oldest passende state_history-Entry
    def _since_fallback(state_type: str, current_value: str) -> Optional[str]:
        if not current_value:
            return None
        row = conn.execute(
            "SELECT ts FROM state_history WHERE character_name=? "
            "AND json_extract(state_json,'$.type')=? "
            "AND json_extract(state_json,'$.value')=? "
            "ORDER BY ts DESC LIMIT 1",
            (character_name, state_type, current_value),
        ).fetchone()
        return row[0] if row else None

    # --- Top-K aktive Memories (Score-basiert, ohne Mutation) ---
    all_mem = load_memories(character_name)
    # completed Commitments standardmaessig raus
    visible = [m for m in all_mem if "completed" not in (m.get("tags") or [])]
    scored = [(_score_memory_no_mutate(m), m) for m in visible]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = []
    for score, m in scored[:12]:
        top.append({
            "id": m.get("id"),
            "memory_type": m.get("memory_type", "semantic"),
            "ts": m.get("timestamp", ""),
            "content": m.get("content", ""),
            "importance": m.get("importance", 3),
            "decay_factor": round(m.get("decay_factor", 1.0), 3),
            "related_character": m.get("related_character", ""),
            "score": round(score, 3),
            "tags": m.get("tags", []),
        })

    # --- Bekannte Orte ---
    # known_locations: None = unrestricted (Character darf alles sehen),
    # Liste = strict. Wir zaehlen pro Ort die Besuche aus state_history.
    known_ids = get_known_locations(character_name)
    visit_rows = conn.execute(
        "SELECT json_extract(state_json,'$.value') AS loc_id, COUNT(*) AS n, "
        "MAX(ts) AS last_ts "
        "FROM state_history WHERE character_name=? "
        "AND json_extract(state_json,'$.type')='location' "
        "GROUP BY loc_id",
        (character_name,),
    ).fetchall()
    visit_by_id = {r[0]: {"count": r[1], "last": r[2]} for r in visit_rows if r[0]}

    if known_ids is None:
        # Legacy/unrestricted: alle Welt-Orte sichtbar (via list_locations,
        # das den richtigen Resolve fuer Klone liefert)
        from app.models.world import list_locations as _list_locs
        all_locs = _list_locs() or []
        known_entries = [
            {"id": loc.get("id", ""), "name": loc.get("name", "")}
            for loc in all_locs if loc.get("id")
        ]
        known_unrestricted = True
    else:
        known_entries = [
            {"id": lid, "name": get_location_name(lid) or lid}
            for lid in known_ids
        ]
        known_unrestricted = False

    for ke in known_entries:
        v = visit_by_id.get(ke["id"], {})
        ke["visit_count"] = v.get("count", 0)
        ke["last_visit"] = v.get("last")
        ke["is_current"] = (ke["id"] == current_location_id)
    # Sortiere: aktueller Ort zuerst, dann nach Besuchen desc, dann Name
    known_entries.sort(
        key=lambda e: (not e["is_current"], -e["visit_count"], e["name"].lower())
    )

    since_activity = (_last_change_ts(activity_lane, current_activity)
                      or _since_fallback("activity", current_activity))
    # Location-Lane ist nach Namens-Aufloesung — direkt gegen die rohe DB
    # vergleichen statt gegen die aufgeloeste Lane.
    since_location = _since_fallback("location", current_location_id)
    since_mood = (mood_lane[-1].get("timestamp") if mood_lane
                  else (full_mood[-1].get("timestamp") if full_mood else None))

    return {
        "character": character_name,
        "now": now.isoformat(),
        "status": {
            "location": location_name or current_location_id,
            "location_id": current_location_id,
            "room": room_name,
            "room_id": current_room_id,
            "activity": current_activity,
            "mood": current_mood,
            "since": {
                "activity": since_activity,
                "location": since_location,
                "mood": since_mood,
            },
            "last_warning": last_warning,
        },
        "known_locations": {
            "unrestricted": known_unrestricted,
            "items": known_entries,
        },
        "lanes_24h": {
            "activity": _bucket_state_lane(activity_lane),
            "location": _bucket_state_lane(location_lane),
            "mood": _bucket_state_lane(
                [{"ts": m.get("timestamp"), "value": m.get("mood")} for m in mood_lane]
            ),
            "effects": _bucket_state_lane(effects_lane),
        },
        "active_memories": top,
    }


@router.get("/{character_name}/memory/list")
def memory_list(character_name: str,
                limit: int = 50,
                offset: int = 0,
                tier: str = "",
                min_importance: int = 0,
                q: str = "",
                related: str = "",
                source: str = "",
                sort: str = "recent",
                include_completed: bool = False) -> Dict[str, Any]:
    """Tab "Erinnerungen": gefilterte, paginierte Memory-Liste + Facets.

    Sort: 'recent' (default) | 'importance' | 'access' | 'score'
    """
    from app.models.memory import load_memories
    from collections import Counter

    all_mem = load_memories(character_name)
    total_unfiltered = len(all_mem)

    # --- Facets aus dem ungefilterten Bestand (fuer Toolbar-Counts) ---
    tier_counts = Counter(m.get("memory_type", "semantic") for m in all_mem)
    src_counts: Counter = Counter()
    rel_counts: Counter = Counter()
    for m in all_mem:
        # Quelle: aus meta abgeleitet — agent-loop schreibt 'thought'/'intent',
        # extraction-Pfad laesst es leer.
        src = "thought" if "thought" in (m.get("tags") or []) else (
            "intent" if "intent" in (m.get("tags") or []) else "extraction"
        )
        # Falls explizit gespeichert (neuerer Code): meta.source nutzen
        # via load_memories liegt es flach im Entry-Dict
        if m.get("source"):
            src = m["source"]
        src_counts[src] += 1
        rc = (m.get("related_character") or "").strip()
        if rc:
            rel_counts[rc] += 1

    # --- Filter ---
    items = all_mem
    if not include_completed:
        items = [m for m in items if "completed" not in (m.get("tags") or [])]
    if tier:
        items = [m for m in items if m.get("memory_type") == tier]
    if min_importance > 0:
        items = [m for m in items if m.get("importance", 3) >= min_importance]
    if related:
        items = [m for m in items if (m.get("related_character") or "") == related]
    if source:
        def _src(m):
            if m.get("source"): return m["source"]
            tags = m.get("tags") or []
            if "thought" in tags: return "thought"
            if "intent" in tags: return "intent"
            return "extraction"
        items = [m for m in items if _src(m) == source]
    if q:
        ql = q.lower()
        items = [m for m in items
                 if ql in (m.get("content") or "").lower()
                 or any(ql in (t or "").lower() for t in (m.get("tags") or []))]

    total = len(items)

    # --- Sort ---
    if sort == "importance":
        items.sort(key=lambda m: (m.get("importance", 3), m.get("timestamp", "")), reverse=True)
    elif sort == "access":
        items.sort(key=lambda m: (m.get("access_count", 0), m.get("timestamp", "")), reverse=True)
    elif sort == "score":
        scored = [(_score_memory_no_mutate(m), m) for m in items]
        scored.sort(key=lambda x: x[0], reverse=True)
        items = [m for _, m in scored]
    else:  # recent
        items.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

    # --- Paginate ---
    page = items[offset:offset + max(1, min(200, limit))]

    return {
        "character": character_name,
        "total": total,
        "total_unfiltered": total_unfiltered,
        "limit": limit,
        "offset": offset,
        "items": page,
        "facets": {
            "tiers": dict(tier_counts),
            "sources": dict(src_counts),
            "related_characters": [
                {"name": n, "count": c}
                for n, c in rel_counts.most_common()
            ],
        },
    }


@router.get("/{character_name}/memory/relationships")
def memory_relationships(character_name: str,
                         history_limit: int = 10) -> Dict[str, Any]:
    """Tab "Beziehungen": Sentiment, Strength, Tension + letzte N Events.

    `memories_count` = wie viele Memories haben diesen Partner als
    related_character gesetzt — Klick im Frontend filtert Tab 2.
    """
    from app.models.relationship import get_character_relationships
    from app.models.memory import load_memories
    from collections import Counter

    rels = get_character_relationships(character_name)
    mem = load_memories(character_name)
    rel_count_by_partner: Counter = Counter(
        (m.get("related_character") or "").strip()
        for m in mem if (m.get("related_character") or "").strip()
    )

    items = []
    for r in rels:
        # Partner = die andere Seite. _row_to_rel fuellt character_a/b mit
        # from_char/to_char (DB-Reihenfolge); wir wollen den Nicht-self-Namen.
        a = r.get("character_a") or ""
        b = r.get("character_b") or ""
        partner = b if a == character_name else a
        # Sentiment aus Sicht des aufrufenden Characters herumdrehen,
        # falls noetig: a_to_b ist Sentiment von a auf b.
        if a == character_name:
            self_sent = r.get("sentiment_a_to_b", 0.0)
            other_sent = r.get("sentiment_b_to_a", 0.0)
        else:
            self_sent = r.get("sentiment_b_to_a", 0.0)
            other_sent = r.get("sentiment_a_to_b", 0.0)
        history = r.get("history") or []
        # Neueste N Events
        recent = history[-history_limit:][::-1] if history else []
        items.append({
            "partner": partner,
            "type": r.get("type", "neutral"),
            "strength": r.get("strength", 10),
            "sentiment_self_to_other": round(self_sent, 3),
            "sentiment_other_to_self": round(other_sent, 3),
            "romantic_tension": round(r.get("romantic_tension", 0.0), 3),
            "interaction_count": r.get("interaction_count", 0),
            "last_interaction": r.get("last_interaction", ""),
            "memories_count": rel_count_by_partner.get(partner, 0),
            "history_recent": recent,
        })
    # Sort: meiste Interaktionen zuerst
    items.sort(key=lambda x: x["interaction_count"], reverse=True)
    return {"character": character_name, "items": items}


def _evolution_diff(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Any]:
    """Zeilenbasiertes Diff (Satz-granular) ueber beliefs/lessons/goals.

    Splittet jedes Feld an Satzgrenzen (`. `, `! `, `? `) und liefert je
    Feld {removed: [str,...], added: [str,...]}.
    """
    import re as _re

    def _split(text: str) -> List[str]:
        if not text: return []
        # Split an Satzgrenzen, behalte aber nicht-leere Stuecke.
        parts = _re.split(r"(?<=[\.!?])\s+", text.strip())
        return [p.strip() for p in parts if p.strip()]

    out = {}
    for field in ("beliefs", "lessons", "goals"):
        a = set(_split(prev.get(field, "")))
        b = set(_split(curr.get(field, "")))
        out[field] = {
            "removed": sorted(list(a - b)),
            "added": sorted(list(b - a)),
        }
    return out


@router.get("/{character_name}/memory/history")
def memory_history(character_name: str,
                   kind: str = "daily",
                   limit: int = 60,
                   offset: int = 0) -> Dict[str, Any]:
    """Tab "Verlauf": daily | weekly | monthly | history | diary | evolution.

    Standard: `daily` (letzte 60 Eintraege ueber alle Partner).
    """
    from app.core.db import get_connection
    import json as _json

    conn = get_connection()

    if kind == "daily":
        rows = conn.execute("""
            SELECT date_key, partner, content
            FROM summaries
            WHERE character_name=? AND kind='daily'
            ORDER BY date_key DESC, partner ASC
            LIMIT ? OFFSET ?
        """, (character_name, limit, offset)).fetchall()
        items = [{"date": r[0], "partner": r[1] or "", "content": r[2]} for r in rows]
        total = conn.execute(
            "SELECT COUNT(*) FROM summaries WHERE character_name=? AND kind='daily'",
            (character_name,),
        ).fetchone()[0]
        return {"character": character_name, "kind": kind,
                "total": total, "limit": limit, "offset": offset, "items": items}

    if kind == "weekly":
        from app.core.memory_service import load_weekly_summaries
        weekly = load_weekly_summaries(character_name)
        items = [{"week": k, "content": v} for k, v in sorted(weekly.items(), reverse=True)]
        return {"character": character_name, "kind": kind, "items": items}

    if kind == "monthly":
        from app.core.memory_service import load_monthly_summaries
        monthly = load_monthly_summaries(character_name)
        items = [{"month": k, "content": v} for k, v in sorted(monthly.items(), reverse=True)]
        return {"character": character_name, "kind": kind, "items": items}

    if kind == "history":
        from app.utils.history_manager import get_cached_summary
        return {"character": character_name, "kind": kind,
                "content": get_cached_summary(character_name) or ""}

    if kind == "diary":
        rows = conn.execute("""
            SELECT id, ts, content, tags
            FROM diary_entries
            WHERE character_name=?
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
        """, (character_name, limit, offset)).fetchall()
        items = []
        for r in rows:
            try: tags = _json.loads(r[3] or "[]")
            except Exception: tags = []
            items.append({"id": r[0], "ts": r[1], "content": r[2], "tags": tags})
        total = conn.execute(
            "SELECT COUNT(*) FROM diary_entries WHERE character_name=?",
            (character_name,),
        ).fetchone()[0]
        return {"character": character_name, "kind": kind,
                "total": total, "limit": limit, "offset": offset, "items": items}

    if kind == "evolution":
        rows = conn.execute("""
            SELECT ts, new_value, reason
            FROM evolution_history
            WHERE character_name=? AND field='snapshot'
            ORDER BY ts ASC
        """, (character_name,)).fetchall()
        snaps = []
        for ts, new_value, reason in rows:
            try: payload = _json.loads(new_value or "{}")
            except Exception: payload = {}
            snaps.append({
                "ts": ts,
                "trigger": payload.get("trigger") or reason or "",
                "beliefs": payload.get("beliefs", ""),
                "lessons": payload.get("lessons", ""),
                "goals": payload.get("goals", ""),
            })
        # Diff jeweils gegen vorherigen Snapshot
        items = []
        prev = None
        for s in snaps:
            diff = _evolution_diff(prev, s) if prev else None
            items.append({**s, "diff": diff})
            prev = s
        # Neueste oben
        items.reverse()
        return {"character": character_name, "kind": kind, "items": items}

    raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")

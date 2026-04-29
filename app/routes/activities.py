"""Activity Library Routes — Zentrale Verwaltung der Aktivitaeten-Bibliothek."""
from fastapi import APIRouter, Request, HTTPException, Query
from typing import Dict, Any, List

from app.core.log import get_logger
from app.models.activity_library import (
    get_all_library_activities,
    get_library_activity,
    save_library_activity,
    delete_library_activity,
    reload_library,
    get_available_activities)

logger = get_logger("activities_routes")

router = APIRouter(prefix="/activities", tags=["activities"])


@router.get("/library")
def list_library() -> Dict[str, Any]:
    """Listet alle Aktivitaeten der Bibliothek (shared + world)."""
    activities = get_all_library_activities()
    # Nach Gruppe gruppieren, alphabetisch sortiert
    groups: Dict[str, List] = {}
    for act in activities:
        group = act.get("_group", "Sonstige")
        groups.setdefault(group, []).append(act)
    # Gruppen alphabetisch, Aktivitaeten innerhalb jeder Gruppe alphabetisch nach Name
    sorted_groups = dict(sorted(groups.items(), key=lambda x: x[0].lower()))
    for group_acts in sorted_groups.values():
        group_acts.sort(key=lambda a: (a.get("name") or "").lower())
    # activities-Liste ebenfalls sortiert
    activities.sort(key=lambda a: ((a.get("_group") or "Sonstige").lower(), (a.get("name") or "").lower()))
    return {"activities": activities, "groups": sorted_groups, "count": len(activities)}


@router.get("/library/{activity_id}")
def get_activity(activity_id: str) -> Dict[str, Any]:
    """Gibt eine einzelne Aktivitaet zurueck."""
    act = get_library_activity(activity_id)
    if not act:
        raise HTTPException(status_code=404, detail="Aktivitaet nicht gefunden")
    return {"activity": act}


@router.post("/library")
async def create_or_update_activity(request: Request) -> Dict[str, Any]:
    """Erstellt oder aktualisiert eine Aktivitaet in der Bibliothek."""
    data = await request.json()
    activity = data.get("activity", {})
    target = data.get("target", "world")  # "shared" oder "world"

    if not activity.get("id") and not activity.get("name"):
        raise HTTPException(status_code=400, detail="id oder name erforderlich")

    if not activity.get("id"):
        activity["id"] = activity["name"].lower().replace(" ", "_")

    save_library_activity(activity, target_dir=target)
    return {"ok": True, "activity": activity}


@router.delete("/library/{activity_id}")
def remove_activity(activity_id: str, target: str = Query("world")) -> Dict[str, Any]:
    """Loescht eine Aktivitaet aus der Bibliothek."""
    deleted = delete_library_activity(activity_id, target_dir=target)
    if not deleted:
        raise HTTPException(status_code=404, detail="Aktivitaet nicht gefunden")
    return {"ok": True}


@router.post("/library/reload")
def reload_library_route() -> Dict[str, Any]:
    """Laedt die Bibliothek neu (nach manuellen Datei-Aenderungen)."""
    reload_library()
    count = len(get_all_library_activities())
    return {"ok": True, "count": count}


@router.get("/available/{character_name}")
def available_activities(
    character_name: str,
    location_id: str = Query(""),
    room_id: str = Query("")) -> Dict[str, Any]:
    """Gibt alle verfuegbaren Aktivitaeten fuer einen Character zurueck.

    Beruecksichtigt: Bibliothek + Location + Character-Overrides + Conditions.
    """

    activities = get_available_activities(character_name,
        location_id=location_id,
        room_id=room_id)
    return {
        "activities": activities,
        "names": [a.get("name", "") for a in activities],
        "count": len(activities),
    }


# --- Extra Activities (Referenz-Zuweisung) ---

@router.get("/extra/{character_name}")
def get_extra_activities(
    character_name: str) -> Dict[str, Any]:
    """Gibt die extra_activities-Liste eines Characters zurueck.

    template_extras = vom Template geerbte Activities (read-only), die
    der Scheduler ohnehin schon mitnimmt. So sieht der Editor, dass man
    diese NICHT nochmal explizit zufuegen muss.
    """
    from app.models.character import get_character_config, get_character_profile
    from app.models.character_template import get_template
    config = get_character_config(character_name)
    profile = get_character_profile(character_name)
    template_extras: list = []
    try:
        tmpl = get_template(profile.get("template", "human-default"))
        if tmpl and isinstance(tmpl.get("extra_activities"), list):
            template_extras = [str(x).strip() for x in tmpl["extra_activities"]
                               if x and str(x).strip()]
    except Exception:
        pass
    return {
        "extra_activities": config.get("extra_activities", []),
        "template_extras": template_extras,
    }


@router.post("/extra/{character_name}")
async def add_extra_activity(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Fuegt eine Bibliotheks-Aktivitaet als Referenz hinzu."""
    data = await request.json()
    user_id = data.get("user_id", "")
    activity_id = data.get("activity_id", "")
    if not activity_id:
        raise HTTPException(status_code=400, detail="user_id und activity_id erforderlich")

    from app.models.character import get_character_config, save_character_config
    config = get_character_config(character_name)
    extras = config.get("extra_activities", [])
    if activity_id not in extras:
        extras.append(activity_id)
        config["extra_activities"] = extras
        save_character_config(character_name, config)
    return {"ok": True, "extra_activities": extras}


@router.post("/extra-broadcast")
async def broadcast_extra_activity(request: Request) -> Dict[str, Any]:
    """Weist eine Bibliotheks-Aktivitaet allen Characters mit gleichem Template zu."""
    data = await request.json()
    user_id = data.get("user_id", "")
    source_character = data.get("source_character", "")
    activity_id = data.get("activity_id", "")

    if not source_character or not activity_id:
        raise HTTPException(status_code=400, detail="user_id, source_character und activity_id erforderlich")

    from app.models.character import (
        list_available_characters, get_character_profile,
        get_character_config, save_character_config)

    # Template des Quell-Characters
    source_profile = get_character_profile(source_character)
    source_template = source_profile.get("template", "")
    if not source_template:
        raise HTTPException(status_code=400, detail="Quell-Character hat kein Template")

    # Allen Characters mit gleichem Template zuweisen
    updated = []
    for char_name in list_available_characters():
        profile = get_character_profile(char_name)
        if profile.get("template", "") != source_template:
            continue
        config = get_character_config(char_name)
        extras = config.get("extra_activities", [])
        if activity_id not in extras:
            extras.append(activity_id)
            config["extra_activities"] = extras
            save_character_config(char_name, config)
            updated.append(char_name)

    return {"ok": True, "updated": updated, "count": len(updated), "template": source_template}


@router.delete("/extra/{character_name}/{activity_id}")
def remove_extra_activity(
    character_name: str,
    activity_id: str) -> Dict[str, Any]:
    """Entfernt eine Bibliotheks-Referenz vom Character."""
    from app.models.character import get_character_config, save_character_config
    config = get_character_config(character_name)
    extras = config.get("extra_activities", [])
    if activity_id in extras:
        extras.remove(activity_id)
        config["extra_activities"] = extras
        save_character_config(character_name, config)
        return {"ok": True, "removed": activity_id}
    raise HTTPException(status_code=404, detail="Aktivitaet nicht zugewiesen")


# --- Overrides ---

@router.get("/overrides/character/{character_name}")
def get_character_overrides(
    character_name: str) -> Dict[str, Any]:
    """Gibt Character-spezifische Activity-Overrides zurueck."""
    from app.models.character import get_character_config
    config = get_character_config(character_name)
    return {
        "activity_overrides": config.get("activity_overrides", {}),
        "extra_activities": config.get("extra_activities", []),
    }


@router.post("/overrides/character/{character_name}")
async def save_character_override(
    character_name: str,
    request: Request) -> Dict[str, Any]:
    """Speichert einen Character-Override fuer eine Aktivitaet."""
    data = await request.json()
    user_id = data.get("user_id", "")
    activity_id = data.get("activity_id", "")
    override = data.get("override", {})

    if not activity_id:
        raise HTTPException(status_code=400, detail="user_id und activity_id erforderlich")

    from app.models.character import get_character_config, save_character_config
    config = get_character_config(character_name)
    overrides = config.get("activity_overrides", {})
    if override:
        overrides[activity_id] = override
        # Auto-Link: Aktivitaet auch in extra_activities aufnehmen
        extras = config.get("extra_activities", [])
        if activity_id not in extras:
            extras.append(activity_id)
            config["extra_activities"] = extras
    elif activity_id in overrides:
        del overrides[activity_id]
    config["activity_overrides"] = overrides
    save_character_config(character_name, config)
    return {"ok": True}


@router.get("/overrides/location/{location_id}")
def get_location_overrides(
    location_id: str) -> Dict[str, Any]:
    """Gibt Location-spezifische Activity-Overrides zurueck."""
    from app.models.world import get_location_by_id
    loc = get_location_by_id(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location nicht gefunden")
    return {"activity_overrides": loc.get("activity_overrides", {})}


@router.post("/overrides/location/{location_id}")
async def save_location_override(
    location_id: str,
    request: Request) -> Dict[str, Any]:
    """Speichert einen Location-Override fuer eine Aktivitaet."""
    data = await request.json()
    user_id = data.get("user_id", "")
    activity_id = data.get("activity_id", "")
    override = data.get("override", {})

    if not activity_id:
        raise HTTPException(status_code=400, detail="user_id und activity_id erforderlich")

    from app.models.world import _load_world_data, _save_world_data
    world = _load_world_data()
    for loc in world.get("locations", []):
        if loc.get("id") == location_id:
            overrides = loc.get("activity_overrides", {})
            if override:
                overrides[activity_id] = override
                # Auto-Link: Aktivitaet in alle Raeume aufnehmen wenn nicht vorhanden
                for room in loc.get("rooms", []):
                    room_acts = room.get("activities", [])
                    if activity_id not in room_acts:
                        room_acts.append(activity_id)
                        room["activities"] = room_acts
            elif activity_id in overrides:
                del overrides[activity_id]
            loc["activity_overrides"] = overrides
            _save_world_data(world)
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Location nicht gefunden")
